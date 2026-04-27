#!/usr/bin/env python3
"""
Claude + DeepSeek → Obsidian Scraper (v4.2)
Extrai artefatos e conversas completas do export do Claude E do DeepSeek.

Uso: python claude_scraper.py <arquivo_export.json> [--vault /caminho/vault]

Como obter os exports:
  Claude   → claude.ai → Settings → Privacy & Data → Export data
  DeepSeek → chat.deepseek.com → Perfil → Export data

Correções v4.2 (links quebrados):
  - Todos os geradores de nota recebem os slugs já resolvidos como parâmetros
    em vez de recalcular com slugify() localmente — elimina a raiz do problema
  - resolve_conv_slugs pré-semeia o UniqueSlugger com slugs existentes no índice:
    conversas antigas não são renomeadas em runs incrementais
  - resolve_artifact_slugs pré-semeia a partir de seen_artifacts + disco:
    sem colisões cross-batch
  - conv_link nas notas de artefato inclui display label:
    [[Conversas/Claude/slug|Título]] em vez de [[Conversas/Claude/slug]]
  - write_vault simplificado: seen_convs não é mais populado duas vezes
"""

import hashlib
import json
import re
import sys
import unicodedata
import argparse
import logging
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional
from collections import defaultdict

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")  # nível ajustado após parse dos args
log = logging.getLogger(__name__)


# ── Constantes ─────────────────────────────────────────────────────────────────

ARTIFACT_TYPES = {
    "application/vnd.ant.code":     ("code",     "💻"),
    "application/vnd.ant.react":    ("react",    "⚛️"),
    "application/vnd.ant.html":     ("html",     "🌐"),
    "application/vnd.ant.markdown": ("markdown", "📝"),
    "application/vnd.ant.mermaid":  ("mermaid",  "📊"),
    "application/vnd.ant.svg":      ("svg",      "🎨"),
}

LANG_EXT = {
    "python": "py", "javascript": "js", "typescript": "ts",
    "bash": "sh", "shell": "sh", "css": "css", "html": "html",
    "sql": "sql", "rust": "rs", "go": "go", "java": "java",
    "cpp": "cpp", "c": "c", "json": "json", "yaml": "yml",
    "toml": "toml", "markdown": "md", "r": "r", "ruby": "rb",
}

ROLE_LABEL_USER = {"human", "user", "request"}
ROLE_LABEL_ASSISTANT = {"assistant", "ai", "response"}

SOURCE_AI_LABEL = {"claude": "Claude", "deepseek": "DeepSeek", "chatgpt": "ChatGPT"}

ROLE_LABEL_STATIC = {
    "human": "Você", "user": "Você", "request": "Você",
    "system": "Sistema", "tool": "🔧 Tool",
}

def role_label(role: str, source: str = "claude") -> str:
    """Retorna o label legível de um role, resolvendo o nome do assistente pelo source."""
    if role in ROLE_LABEL_ASSISTANT:
        return SOURCE_AI_LABEL.get(source, source.capitalize())
    return ROLE_LABEL_STATIC.get(role, role.capitalize())

INDEX_FILE      = ".claude_artifact_ids.json"
CONV_INDEX_FILE = ".claude_conv_ids.json"


# ── Detecção de formato ────────────────────────────────────────────────────────

def detect_format(data) -> str:
    conversations = data if isinstance(data, list) else data.get("conversations", [data])
    if not conversations:
        return "claude"
    first = conversations[0]
    if "mapping" in first:
        for node in first["mapping"].values():
            msg = node.get("message") or {}
            for frag in msg.get("fragments", []):
                if frag.get("type") in ("REQUEST", "RESPONSE", "THINK"):
                    return "deepseek"
    return "claude"


# ── Dataclasses ────────────────────────────────────────────────────────────────

@dataclass
class ArtifactVersion:
    content: str
    message_index: int
    created_at: Optional[datetime] = None
    command: str = "create"


@dataclass
class Branch:
    """Caminho alternativo em uma bifurcação da conversa."""
    fork_index: int          # índice da mensagem onde ocorreu a bifurcação
    fork_preview: str        # prévia do conteúdo no ponto de bifurcação
    messages: list           # lista de Message do caminho alternativo
    branch_num: int = 1      # número da alternativa (1, 2, ...)


@dataclass
class Artifact:
    id: str
    title: str
    type: str
    content: str
    language: Optional[str] = None
    conversation_title: str = ""
    conversation_id: str = ""
    created_at: Optional[datetime] = None
    message_index: int = 0
    context_before: str = ""
    versions: list = field(default_factory=list)


@dataclass
class Message:
    role: str
    text: str
    created_at: Optional[datetime] = None
    artifacts: list = field(default_factory=list)
    has_artifacts: bool = False
    think_text: str = ""
    model: str = ""


@dataclass
class ConversationMeta:
    id: str
    title: str
    created_at: Optional[datetime] = None
    artifacts: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    source: str = "claude"
    branches: list = field(default_factory=list)  # lista de Branch


# ── Helpers ────────────────────────────────────────────────────────────────────

def parse_datetime(s: str) -> Optional[datetime]:
    if not s:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    log.debug("parse_datetime: formato não reconhecido para '%s'", s)
    return None


def slugify(text: str, max_len: int = 60) -> str:
    text = unicodedata.normalize("NFC", text)
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^\w\s\-]", "", text, flags=re.UNICODE)
    text = re.sub(r"\s+", "-", text.strip())
    text = re.sub(r"-+", "-", text)
    return text[:max_len].strip("-") or "sem-titulo"


class UniqueSlugger:
    """
    Gera slugs únicos dentro de um escopo.
    seed() pré-registra slugs existentes para que runs incrementais
    não renomeiem entradas antigas.
    """
    def __init__(self):
        self._seen: dict[str, int] = {}

    def seed(self, slug: str):
        if slug and slug not in self._seen:
            self._seen[slug] = 1

    def make(self, text: str, max_len: int = 60, _origin: str = "") -> str:
        base = slugify(text, max_len=max_len)
        if base == "sem-titulo":
            log.debug("Slug 'sem-titulo' gerado%s | título bruto: %r",
                      f" (origem: {_origin})" if _origin else "",
                      text)
        if base not in self._seen:
            self._seen[base] = 1
            return base
        count = self._seen[base] + 1
        self._seen[base] = count
        suffix = f"-{count}"
        return base[: max_len - len(suffix)] + suffix


def extract_context(text: str, max_chars: int = 300) -> str:
    text = text.strip()
    if not text:
        return ""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    context = paragraphs[-1] if paragraphs else text
    if len(context) > max_chars:
        cut = context[-(max_chars - 1):]
        cut = cut.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
        context = "…" + cut
    return context


def ext_to_mime(path: str) -> str:
    ext = Path(path).suffix.lower().lstrip(".")
    mapping = {
        "py": "application/vnd.ant.code",  "js": "application/vnd.ant.code",
        "ts": "application/vnd.ant.code",  "jsx": "application/vnd.ant.react",
        "tsx": "application/vnd.ant.react","html": "application/vnd.ant.html",
        "htm": "application/vnd.ant.html", "md": "application/vnd.ant.markdown",
        "svg": "application/vnd.ant.svg",  "sh": "application/vnd.ant.code",
        "bat": "application/vnd.ant.code", "ps1": "application/vnd.ant.code",
        "sql": "application/vnd.ant.code", "json": "application/vnd.ant.code",
        "yaml": "application/vnd.ant.code","yml": "application/vnd.ant.code",
        "toml": "application/vnd.ant.code","rs": "application/vnd.ant.code",
        "go": "application/vnd.ant.code",  "c": "application/vnd.ant.code",
        "cpp": "application/vnd.ant.code", "css": "application/vnd.ant.code",
    }
    return mapping.get(ext, "application/vnd.ant.code")


def type_label(artifact: Artifact) -> tuple[str, str]:
    type_key = artifact.type
    if type_key in ARTIFACT_TYPES:
        return ARTIFACT_TYPES[type_key]
    if "code" in type_key:
        return ("code", "💻")
    if "react" in type_key:
        return ("react", "⚛️")
    return ("outros", "📄")


# ── Parser de blocos (Claude) ──────────────────────────────────────────────────

def find_artifacts_in_content(content_blocks: list) -> list[dict]:
    found = []
    for block in content_blocks:
        btype = block.get("type", "")

        if btype == "tool_use" and block.get("name") == "artifacts":
            inp = block.get("input", {})
            if inp.get("command") in ("create", "rewrite", None) and inp.get("content"):
                found.append(inp)

        elif btype == "tool_use" and block.get("name") == "create_file":
            inp = block.get("input", {})
            path = inp.get("path", "")
            file_text = inp.get("file_text", "")
            if not file_text or not path:
                continue
            if any(skip in path for skip in ["/tmp/", "node_modules", "__pycache__"]):
                continue
            ext = Path(path).suffix.lower().lstrip(".")
            filename = Path(path).name
            found.append({
                "id": f"cf_{hashlib.md5(path.encode(), usedforsecurity=False).hexdigest()[:12]}",
                "type": ext_to_mime(path),
                "language": ext if ext else "text",
                "title": inp.get("description", filename) or filename,
                "content": file_text,
                "command": "create",
            })

        elif btype == "tool_use" and block.get("name") == "visualize:show_widget":
            inp = block.get("input", {})
            code = inp.get("widget_code", "")
            title = inp.get("title", "widget")
            if not code:
                continue
            mime = "application/vnd.ant.svg" if code.strip().startswith("<svg") else "application/vnd.ant.html"
            found.append({
                "id": f"widget_{hashlib.md5((title + code[:40]).encode(), usedforsecurity=False).hexdigest()[:12]}",
                "type": mime,
                "language": "svg" if mime.endswith("svg") else "html",
                "title": title.replace("_", " ").title(),
                "content": code,
                "command": "create",
            })

        elif btype == "artifact":
            found.append(block)

    return found


def extract_text_from_blocks(content_blocks: list) -> str:
    """
    Extrai texto dos blocos de conteúdo de uma mensagem Claude.
    Captura inline ferramentas relevantes e blocos de raciocínio (thinking).
    """
    parts = []
    for block in content_blocks:
        btype = block.get("type", "")

        if btype == "text":
            t = block.get("text", "").strip()
            if t:
                parts.append(t)

        elif btype == "thinking":
            thinking = block.get("thinking", "").strip()
            if thinking:
                lines = "\n".join(f"> {l}" for l in thinking.splitlines())
                parts.append(f"> [!note]- 🧠 Raciocínio interno *(clique para expandir)*\n{lines}")

        elif btype == "tool_use":
            name = block.get("name", "")
            inp  = block.get("input", {})

            if name == "web_search":
                query = inp.get("query", "").strip()
                if query:
                    parts.append(f"> 🔍 **Busca web:** `{query}`")

            elif name == "web_fetch":
                url = inp.get("url", "").strip()
                if url:
                    parts.append(f"> 🌐 **Web fetch:** {url}")

            elif name == "bash_tool":
                cmd  = inp.get("command", "").strip()
                desc = inp.get("description", "").strip()
                if cmd:
                    header = f"> 💻 **Terminal**" + (f": {desc}" if desc else "")
                    parts.append(f"{header}\n> ```bash\n> {cmd}\n> ```")

            elif name == "present_files":
                paths = inp.get("filepaths", [])
                if paths:
                    names = [Path(p).name for p in paths]
                    files_str = ", ".join(f"`{n}`" for n in names)
                    parts.append(f"> 📎 **Arquivos gerados:** {files_str}")

            elif name == "image_search":
                query = inp.get("query", "").strip()
                if query:
                    parts.append(f"> 🖼️ **Busca de imagens:** `{query}`")

            elif name == "tool_search":
                query = inp.get("query", "").strip()
                if query:
                    parts.append(f"> 🔧 **Busca de ferramentas:** `{query}`")

            elif name.startswith("Notion:"):
                action = name.split(":")[-1].replace("-", " ").title()
                detail = ""
                if "search" in name:
                    detail = inp.get("query", "")
                elif "fetch" in name or "update" in name:
                    detail = inp.get("id", inp.get("page_id", ""))
                elif "create" in name:
                    pages = inp.get("pages", [])
                    titles = [p.get("properties", {}).get("title", "") for p in pages[:2]]
                    detail = ", ".join(t for t in titles if t)
                suffix = f": `{detail}`" if detail else ""
                parts.append(f"> 📓 **Notion — {action}**{suffix}")

            elif name in ("event_create_v1", "event_update_v0", "event_search_v0"):
                action = {"event_create_v1": "criar evento", "event_update_v0": "atualizar evento",
                          "event_search_v0": "buscar eventos"}.get(name, name)
                events = inp.get("new_events", inp.get("event_updates", []))
                if events:
                    titles = [e.get("title", "") for e in events[:2] if e.get("title")]
                    detail = ", ".join(f"`{t}`" for t in titles) if titles else ""
                    parts.append(f"> 📅 **Calendário — {action}**" + (f": {detail}" if detail else ""))
                else:
                    start = inp.get("start_time", "")[:10]
                    end   = inp.get("end_time", "")[:10]
                    if start:
                        parts.append(f"> 📅 **Calendário — {action}:** `{start}` → `{end}`")

    return "\n\n".join(parts)


def extract_text_before(content_blocks: list, artifact_id: str) -> str:
    text_acc = []
    for block in content_blocks:
        btype = block.get("type", "")
        if btype == "text":
            text_acc.append(block.get("text", ""))
        elif btype == "tool_use" and block.get("name") == "artifacts":
            if block.get("input", {}).get("id") == artifact_id:
                break
    return extract_context(" ".join(text_acc))


# ── Parser principal — Claude ──────────────────────────────────────────────────

def parse_claude_export(conversations_raw: list) -> list[ConversationMeta]:
    result: list[ConversationMeta] = []

    for conv in conversations_raw:
        conv_id    = conv.get("uuid", conv.get("id", "unknown"))
        conv_title = conv.get("name", conv.get("title", "")).strip()
        conv_date  = parse_datetime(conv.get("created_at", ""))

        meta = ConversationMeta(id=conv_id, title=conv_title, created_at=conv_date, source="claude")
        artifact_registry: dict[str, Artifact] = {}

        for idx, msg in enumerate(conv.get("chat_messages", conv.get("messages", []))):
            sender   = msg.get("sender", msg.get("role", "")).lower()
            msg_date = parse_datetime(msg.get("created_at", ""))

            content_blocks = msg.get("content", [])
            if not content_blocks and msg.get("text"):
                content_blocks = [{"type": "text", "text": msg["text"]}]

            msg_text = extract_text_from_blocks(content_blocks)

            # Fallback de título: usa o início da primeira mensagem do usuário
            if not meta.title and sender in ("human", "user") and msg_text:
                snippet = msg_text.strip().splitlines()[0][:60].strip()
                if snippet:
                    meta.title = f"📝 {snippet}"
            if not msg_text and msg.get("text"):
                msg_text = msg["text"].strip()

            artifact_ids_in_msg = []

            if sender in ("assistant", "ai"):
                for art_raw in find_artifacts_in_content(content_blocks):
                    art_type    = art_raw.get("type", "application/vnd.ant.code")
                    art_id      = art_raw.get("id", f"art_{idx}")
                    art_title   = (art_raw.get("title") or "").strip() or f"Artefato {idx+1}"
                    art_content = art_raw.get("content", "")
                    art_lang    = art_raw.get("language") or ""
                    command     = art_raw.get("command", "create")
                    context     = extract_text_before(content_blocks, art_id)

                    version = ArtifactVersion(
                        content=art_content,
                        message_index=idx,
                        created_at=msg_date or conv_date,
                        command=command,
                    )

                    if art_id in artifact_registry:
                        existing = artifact_registry[art_id]
                        existing.versions.append(version)
                        existing.content = art_content
                        is_generic = re.fullmatch(r"Artefato\s+\d+", art_title) is not None
                        if art_title and not is_generic:
                            existing.title = art_title
                        log.info("Artefato reescrito: '%s' (id=%s, versão %d)",
                                 art_title, art_id, len(existing.versions) + 1)
                    else:
                        artifact = Artifact(
                            id=art_id, title=art_title, type=art_type,
                            content=art_content,
                            language=art_lang.lower() if art_lang else None,
                            conversation_title=conv_title, conversation_id=conv_id,
                            created_at=msg_date or conv_date,
                            message_index=idx, context_before=context,
                            versions=[version],
                        )
                        artifact_registry[art_id] = artifact
                        meta.artifacts.append(artifact)

                    artifact_ids_in_msg.append(art_id)

            if msg_text or artifact_ids_in_msg:
                meta.messages.append(Message(
                    role=sender, text=msg_text,
                    created_at=msg_date or conv_date,
                    artifacts=artifact_ids_in_msg,
                    has_artifacts=bool(artifact_ids_in_msg),
                ))

        result.append(meta)
    return result


# ── Parser principal — DeepSeek ────────────────────────────────────────────────

def _walk_deepseek_tree(mapping: dict) -> tuple[list[dict], list[tuple[int, str, list[dict]]]]:
    """
    Percorre a árvore do DeepSeek em ordem linear seguindo o último filho.
    Retorna (mensagens_principais, ramificações).

    Cada ramificação é uma tupla (fork_index, fork_preview, [mensagens_alternativas]).
    fork_index = posição na lista principal onde ocorreu a bifurcação.
    """
    node = mapping.get("root")
    if not node:
        return [], []

    messages: list[dict] = []
    branches: list[tuple[int, str, list[dict]]] = []
    visited = set()

    def collect_subtree(start_id: str) -> list[dict]:
        """Coleta todas as mensagens de um sub-caminho linearmente."""
        result = []
        nid = start_id
        seen = set()
        while nid and nid not in seen:
            seen.add(nid)
            n = mapping.get(nid)
            if not n:
                break
            m = n.get("message")
            if m and m.get("fragments"):
                result.append(m)
            children = n.get("children", [])
            nid = children[-1] if children else None
        return result

    while node:
        node_id = node.get("id")
        if node_id in visited:
            break
        visited.add(node_id)

        msg = node.get("message")
        if msg and msg.get("fragments"):
            messages.append(msg)

        children = node.get("children", [])
        if not children:
            break

        if len(children) > 1:
            fork_index = len(messages) - 1
            fork_msg   = messages[-1] if messages else {}
            # Prévia do ponto de bifurcação
            frags = (fork_msg.get("fragments") or [])
            fork_preview = next(
                (f.get("content", "")[:80] for f in frags
                 if f.get("type") in ("RESPONSE", "REQUEST") and f.get("content")),
                ""
            )
            # Todos os filhos exceto o último (caminho principal)
            for alt_id in children[:-1]:
                alt_msgs = collect_subtree(alt_id)
                if alt_msgs:
                    branches.append((fork_index, fork_preview, alt_msgs))

        node = mapping.get(children[-1])

    return messages, branches


def parse_deepseek_export(conversations_raw: list) -> list[ConversationMeta]:
    result: list[ConversationMeta] = []
    for conv in conversations_raw:
        conv_id    = conv.get("id", conv.get("uuid", "unknown"))
        conv_title = conv.get("title", "").strip()
        conv_date  = parse_datetime(conv.get("inserted_at", conv.get("created_at", "")))
        meta = ConversationMeta(id=conv_id, title=conv_title, created_at=conv_date, source="deepseek")

        raw_messages, raw_branches = _walk_deepseek_tree(conv.get("mapping", {}))

        if len(raw_branches) > 0:
            total_forks = sum(1 for node in conv.get("mapping", {}).values()
                              if len(node.get("children", [])) > 1)
            log.info("Conversa '%s': %d bifurcação(ões), capturando todos os caminhos.",
                     conv_title, total_forks)

        def parse_ds_msgs(msgs: list[dict]) -> list[Message]:
            parsed = []
            for msg in msgs:
                msg_date = parse_datetime(msg.get("inserted_at", ""))
                model    = msg.get("model", "")
                think_parts, request_parts, response_parts = [], [], []
                for frag in msg.get("fragments", []):
                    ftype   = frag.get("type", "").upper()
                    content = frag.get("content", "").strip()
                    if not content and ftype not in ("SEARCH", "TOOL_SEARCH", "TOOL_OPEN", "READ_LINK"):
                        continue
                    if ftype == "THINK":
                        think_parts.append(content)
                    elif ftype == "REQUEST":
                        request_parts.append(content)
                    elif ftype == "RESPONSE":
                        response_parts.append(content)
                    elif ftype == "SEARCH":
                        results = frag.get("results", [])
                        if results:
                            lines = [f"> 🔍 **DeepSeek Search**"]
                            for r in results[:5]:
                                title = r.get("title", "").strip()
                                url   = r.get("url", "").strip()
                                if title and url:
                                    lines.append(f"> - [{title}]({url})")
                                elif url:
                                    lines.append(f"> - {url}")
                            response_parts.append("\n".join(lines))
                    elif ftype == "TOOL_SEARCH":
                        query = frag.get("query", frag.get("content", "")).strip()
                        if query:
                            response_parts.append(f"> 🔧 **DeepSeek Tool Search:** `{query}`")
                    elif ftype == "TOOL_OPEN":
                        url = frag.get("url", frag.get("content", "")).strip()
                        if url:
                            response_parts.append(f"> 🌐 **DeepSeek Open:** {url}")
                    elif ftype == "READ_LINK":
                        url = frag.get("url", frag.get("content", "")).strip()
                        if url:
                            response_parts.append(f"> 📖 **DeepSeek Read:** {url}")
                if request_parts:
                    parsed.append(Message(
                        role="request", text="\n\n".join(request_parts),
                        created_at=msg_date or conv_date, model=model,
                    ))
                if response_parts or think_parts:
                    parsed.append(Message(
                        role="response", text="\n\n".join(response_parts),
                        created_at=msg_date or conv_date,
                        think_text="\n\n".join(think_parts), model=model,
                    ))
            return parsed

        meta.messages = parse_ds_msgs(raw_messages)

        # Fallback de título: usa início da primeira mensagem do usuário
        if not meta.title:
            for m in meta.messages:
                if m.role in ("request", "user", "human") and m.text:
                    snippet = m.text.strip().splitlines()[0][:60].strip()
                    if snippet:
                        meta.title = f"📝 {snippet}"
                        break

        # Converte ramificações brutas em objetos Branch
        for fork_idx, fork_preview, alt_msgs in raw_branches:
            branch_msgs = parse_ds_msgs(alt_msgs)
            if branch_msgs:
                meta.branches.append(Branch(
                    fork_index=fork_idx,
                    fork_preview=fork_preview,
                    messages=branch_msgs,
                    branch_num=len(meta.branches) + 1,
                ))

        result.append(meta)
    return result


# ── Parser principal — ChatGPT ─────────────────────────────────────────────────

def parse_datetime_unix(ts) -> Optional[datetime]:
    """Converte timestamp Unix (float/int) para datetime UTC."""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def _walk_chatgpt_tree(mapping: dict, current_node: str) -> tuple[list[dict], list[tuple[int, str, list[dict]]]]:
    """
    Percorre a árvore do ChatGPT de trás para frente a partir de current_node,
    depois inverte para ordem cronológica.
    Retorna (mensagens_principais, ramificações).
    """
    # Primeiro: coleta o caminho principal (current_node → root)
    path, node_id, visited = [], current_node, set()
    while node_id and node_id not in visited:
        visited.add(node_id)
        node = mapping.get(node_id)
        if not node:
            break
        msg = node.get("message")
        if msg:
            role = (msg.get("author") or {}).get("role", "")
            if role != "system":
                path.append((node_id, msg))
        node_id = node.get("parent")
    path.reverse()
    main_ids = {nid for nid, _ in path}

    def collect_subtree(start_id: str) -> list[dict]:
        """Coleta mensagens de um sub-caminho seguindo o último filho."""
        result = []
        nid = start_id
        seen = set()
        while nid and nid not in seen:
            seen.add(nid)
            n = mapping.get(nid)
            if not n:
                break
            m = n.get("message")
            if m:
                role = (m.get("author") or {}).get("role", "")
                if role != "system":
                    result.append(m)
            children = n.get("children", [])
            nid = children[-1] if children else None
        return result

    # Segundo: detecta bifurcações ao longo do caminho principal
    branches: list[tuple[int, str, list[dict]]] = []
    for idx, (nid, msg) in enumerate(path):
        node = mapping.get(nid, {})
        children = node.get("children", [])
        if len(children) > 1:
            content = msg.get("content") or {}
            parts = content.get("parts", [])
            fork_preview = str(parts[0])[:80] if parts and parts[0] else ""
            # Filhos que NÃO estão no caminho principal
            for cid in children:
                if cid not in main_ids:
                    alt_msgs = collect_subtree(cid)
                    if alt_msgs:
                        branches.append((idx, fork_preview, alt_msgs))

    main_msgs = [msg for _, msg in path]
    return main_msgs, branches


def _extract_chatgpt_text(content: dict) -> str:
    """Extrai texto de um bloco de conteúdo do ChatGPT (múltiplos content_type)."""
    if not content:
        return ""
    ctype = content.get("content_type", "text")
    parts = content.get("parts", [])

    if ctype == "text":
        return "\n".join(str(p) for p in parts if p and isinstance(p, str)).strip()

    if ctype == "multimodal_text":
        out = []
        for p in parts:
            if isinstance(p, str) and p.strip():
                out.append(p.strip())
            elif isinstance(p, dict):
                ptype = p.get("content_type", "")
                if ptype == "text":
                    t = p.get("text", "").strip()
                    if t:
                        out.append(t)
                elif ptype in ("image_asset_pointer", "audio_asset_pointer"):
                    kind   = "🖼️ Imagem" if "image" in ptype else "🔊 Áudio"
                    width  = p.get("width", "")
                    height = p.get("height", "")
                    dims   = f" ({width}×{height})" if width and height else ""
                    out.append(f"*[{kind}{dims} — arquivo do export]*")
        return "\n".join(out)

    if ctype == "code":
        lang = content.get("language", "")
        code = content.get("text", "")
        return f"```{lang}\n{code}\n```" if code else ""

    if ctype == "execution_output":
        text = content.get("text", "")
        return f"```\n{text}\n```" if text else ""

    if ctype == "tether_quote":
        title = content.get("title", "")
        text  = content.get("text", "")
        url   = content.get("url", "")
        out   = []
        if title: out.append(f"**{title}**")
        if text:  out.append(text)
        if url:   out.append(f"[Fonte]({url})")
        return "\n".join(out)

    if ctype == "tether_browsing_display":
        summary = (content.get("summary") or "").strip()
        result  = (content.get("result")  or "").strip()
        if summary:
            return f"*[Busca web]*\n{summary}"
        if result:
            lines   = result.splitlines()
            preview = "\n".join(lines[:10])
            suffix  = "\n…" if len(lines) > 10 else ""
            return f"*[Busca web]*\n{preview}{suffix}"
        return ""

    # Fallback genérico
    return "\n".join(str(p) for p in parts if p and isinstance(p, str)).strip()


def parse_chatgpt_export(conversations_raw: list) -> list[ConversationMeta]:
    result: list[ConversationMeta] = []

    for conv in conversations_raw:
        conv_id      = conv.get("conversation_id", conv.get("id", "unknown"))
        conv_title   = (conv.get("title") or "Conversa sem título").strip()
        conv_date    = parse_datetime_unix(conv.get("create_time"))
        current_node = conv.get("current_node", "")
        mapping      = conv.get("mapping", {})

        meta = ConversationMeta(
            id=conv_id, title=conv_title, created_at=conv_date, source="chatgpt"
        )

        raw_messages, raw_branches = _walk_chatgpt_tree(mapping, current_node)

        def parse_gpt_msg(msg: dict, conv_date=conv_date) -> Optional[Message]:
            role     = (msg.get("author") or {}).get("role", "")
            msg_date = parse_datetime_unix(msg.get("create_time"))
            text     = _extract_chatgpt_text(msg.get("content") or {})
            if role == "user":
                mapped_role = "user"
            elif role == "assistant":
                mapped_role = "assistant"
            elif role == "tool":
                if not text:
                    return None
                mapped_role = "tool"
            else:
                return None
            if not text:
                return None
            return Message(role=mapped_role, text=text, created_at=msg_date or conv_date)

        for msg in raw_messages:
            m = parse_gpt_msg(msg)
            if m:
                meta.messages.append(m)

        # Converte ramificações brutas em objetos Branch
        for fork_idx, fork_preview, alt_msgs in raw_branches:
            branch_msgs = [m for m in (parse_gpt_msg(msg) for msg in alt_msgs) if m]
            if branch_msgs:
                meta.branches.append(Branch(
                    fork_index=fork_idx,
                    fork_preview=fork_preview,
                    messages=branch_msgs,
                    branch_num=len(meta.branches) + 1,
                ))

        result.append(meta)
    return result


def _chatgpt_markers_in_dir(folder: Path) -> bool:
    """
    Verifica se uma pasta é um export ChatGPT.

    Estratégia de dois critérios obrigatórios:
    1. Tem arquivos paginados conversations-NNN.json (exclusivo do ChatGPT)
    2. Tem ao menos um marcador forte que não existe em Claude/DeepSeek

    Exigir os dois juntos elimina falsos positivos:
    - DeepSeek pode ter user.json mas NÃO tem conversations-000.json
    - Uma pasta aleatória pode ter chat.html mas NÃO tem conversations-000.json
    """
    # Critério 1: arquivos paginados (formato exclusivo do ChatGPT)
    has_paginated = bool(list(folder.glob("conversations-[0-9]*.json")))
    if not has_paginated:
        return False

    # Critério 2: ao menos um marcador forte presente junto com os paginados
    # (confirma que é ChatGPT e não outra coisa com arquivos numerados)
    STRONG_MARKERS = {
        "export_manifest.json",      # gerado pelo ChatGPT
        "message_feedback.json",     # gerado pelo ChatGPT
        "shared_conversations.json", # gerado pelo ChatGPT
    }
    for marker in STRONG_MARKERS:
        if (folder / marker).exists():
            return True

    # Paginado encontrado mas sem marcador forte: ainda provável ChatGPT
    # Confirma via estrutura do JSON (current_node)
    return False


def _is_chatgpt_content(convs: list) -> bool:
    """
    Verifica se uma lista de conversas tem estrutura interna do ChatGPT.
    Usa 'current_node' como critério único — campo exclusivo do ChatGPT
    que não existe em exports Claude nem DeepSeek.
    """
    if not convs:
        return False
    # Verifica as primeiras 3 conversas para robustez
    for conv in convs[:3]:
        if "current_node" in conv:
            return True
    return False


def _load_chatgpt_conversations(export_path: Path) -> list[dict]:
    """
    Carrega conversas de um export ChatGPT (pasta paginada ou arquivo único).
    Sempre opera sobre a pasta diretamente — nunca sobe para a pasta-pai.
    """
    folder = export_path if export_path.is_dir() else export_path.parent

    conv_files = sorted(folder.glob("conversations-[0-9]*.json"))
    if not conv_files:
        single = folder / "conversations.json"
        if single.exists():
            conv_files = [single]
    if not conv_files and not export_path.is_dir():
        # Último recurso: o próprio arquivo passado
        conv_files = [export_path]
    if not conv_files:
        raise ValueError(f"Nenhum conversations*.json em {folder}")

    all_convs: list[dict] = []
    for f in conv_files:
        with open(f, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            all_convs.extend(data)
        elif isinstance(data, dict):
            all_convs.extend(data.get("conversations", [data]))
    return all_convs


def _is_chatgpt_export(export_path: Path) -> bool:
    """
    Detecção de export ChatGPT.

    Regra: verifica APENAS a pasta do próprio arquivo/diretório informado,
    nunca a pasta-pai — evita falso positivo quando múltiplos exports de
    fontes diferentes estão lado a lado no mesmo diretório.
    """
    folder = export_path if export_path.is_dir() else export_path.parent

    # 1. Marcadores de arquivo na pasta do export
    if _chatgpt_markers_in_dir(folder):
        log.debug("ChatGPT detectado por marcador de arquivo em: %s", folder)
        return True

    # 2. Verifica conteúdo do JSON
    try:
        if export_path.is_dir():
            conv_files = sorted(export_path.glob("conversations-[0-9]*.json"))
            if not conv_files:
                return False
            target = conv_files[0]
        else:
            target = export_path
        with open(target, encoding="utf-8") as f:
            data = json.load(f)
        convs = data if isinstance(data, list) else data.get("conversations", [])
        result = _is_chatgpt_content(convs)
        if result:
            log.debug("ChatGPT detectado por estrutura JSON em: %s", target)
        return result
    except Exception:
        return False


# ── Entry-point de parsing ─────────────────────────────────────────────────────

def parse_export(export_path: Path) -> tuple[list[ConversationMeta], str]:
    # ChatGPT pode ser pasta — testa antes de tentar abrir como arquivo
    if _is_chatgpt_export(export_path):
        conversations_raw = _load_chatgpt_conversations(export_path)
        return parse_chatgpt_export(conversations_raw), "chatgpt"

    with open(export_path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        conversations_raw = data
    elif isinstance(data, dict):
        conversations_raw = data.get("conversations", data.get("chats", [data]))
    else:
        raise ValueError("Formato de export não reconhecido.")
    fmt = detect_format(data)
    if fmt == "deepseek":
        return parse_deepseek_export(conversations_raw), "deepseek"
    return parse_claude_export(conversations_raw), "claude"


# ── Resolução de slugs ─────────────────────────────────────────────────────────

def resolve_conv_slugs(
    conversations: list[ConversationMeta],
    seen_convs: dict,
) -> dict[str, str]:
    """
    {conv.id → slug}.
    Pré-semeia com slugs do índice para não renomear conversas antigas.
    """
    sluggers: dict[str, UniqueSlugger] = defaultdict(UniqueSlugger)
    result: dict[str, str] = {}

    for conv_id, entry in seen_convs.items():
        if not isinstance(entry, dict):
            continue
        path   = entry.get("path", "")
        source = entry.get("source", "claude")
        slug   = Path(path).name if path else ""
        if slug:
            sluggers[source].seed(slug)
            result[conv_id] = slug

    for meta in conversations:
        if meta.id not in result:
            # Último recurso: se título ainda vazio, usa a data
            if not meta.title:
                date_str = meta.created_at.strftime("%Y-%m-%d") if meta.created_at else "sem-data"
                meta.title = f"Conversa {date_str}"
                log.debug("Conversa %s sem título nem mensagens de usuário; usando data como título.", meta.id)
            result[meta.id] = sluggers[meta.source].make(meta.title, _origin=f"conversa:{meta.id}")

    return result


def resolve_artifact_slugs(
    conversations: list[ConversationMeta],
    seen_artifacts: dict,
    incremental: bool,
    vault_path: Path,
    dry_run: bool,
) -> dict[str, str]:
    """
    {artifact.id → "Artefatos/tipo/slug"}.
    Pré-semeia a partir do índice persistido e do disco.
    """
    sluggers: dict[str, UniqueSlugger] = defaultdict(UniqueSlugger)
    result: dict[str, str] = {}

    # Pré-semeia do índice (fonte primária)
    for art_id, path_str in seen_artifacts.items():
        p = Path(path_str)
        if len(p.parts) >= 3 and p.parts[0] == "Artefatos":
            sluggers[p.parts[1]].seed(p.stem)

    # Complementa com o disco
    if not dry_run:
        artifacts_root = vault_path / "Artefatos"
        if artifacts_root.exists():
            for type_dir in artifacts_root.iterdir():
                if type_dir.is_dir():
                    for f in type_dir.glob("*.md"):
                        sluggers[type_dir.name].seed(f.stem)

    for meta in conversations:
        for art in meta.artifacts:
            if art.id in result:
                continue
            if incremental and art.id in seen_artifacts:
                result[art.id] = seen_artifacts[art.id].removesuffix(".md")
                continue
            atype, _ = type_label(art)
            result[art.id] = f"Artefatos/{atype}/{sluggers[atype].make(art.title, _origin=f"artefato:{art.id}")}"

    return result


# ── Gerador de notas — Artefatos ──────────────────────────────────────────────

def make_code_block(artifact: Artifact) -> str:
    atype, _ = type_label(artifact)
    if atype == "markdown":
        return artifact.content
    if atype == "mermaid":
        return f"```mermaid\n{artifact.content}\n```"
    if atype == "svg":
        return f"```xml\n{artifact.content}\n```"
    if atype in ("react", "html"):
        lang = "jsx" if atype == "react" else "html"
        return f"```{lang}\n{artifact.content}\n```"
    return f"```{artifact.language or 'text'}\n{artifact.content}\n```"


def artifact_to_note(
    artifact: Artifact,
    conv_path: str,   # "Conversas/Claude/minha-conversa"
    conv_title: str,  # "Minha Conversa"
) -> str:
    atype, emoji = type_label(artifact)
    date_str = artifact.created_at.strftime("%Y-%m-%d") if artifact.created_at else "sem-data"
    time_str = artifact.created_at.strftime("%Y-%m-%dT%H:%M:%S") if artifact.created_at else ""
    tags = ["claude-artifact", f"tipo/{atype}"]
    if artifact.language:
        tags.append(f"linguagem/{artifact.language}")
    frontmatter_tags = "\n".join(f"  - {t}" for t in tags)
    code_block   = make_code_block(artifact)
    has_rewrites = len(artifact.versions) > 1
    wikilink     = f"[[{conv_path}|{conv_title}]]"

    note = f"""---
title: "{artifact.title.replace('"', "'")}"
type: {atype}
emoji: {emoji}
language: {artifact.language or ""}
source_conversation: "{wikilink}"
conversation_id: {artifact.conversation_id}
artifact_id: {artifact.id}
created: {time_str}
version_count: {len(artifact.versions)}
tags:
{frontmatter_tags}
---

# {emoji} {artifact.title}

> [!info] Origem
> **Conversa:** {wikilink}
> **Data:** {date_str}
> **Tipo:** `{atype}`{"  |  **Linguagem:** `" + artifact.language + "`" if artifact.language else ""}
{"  |  **Reescrito:** " + str(len(artifact.versions) - 1) + "x" if has_rewrites else ""}

"""

    if artifact.context_before:
        note += f"> [!quote] Contexto\n> {artifact.context_before}\n\n"

    note += f"## Conteúdo\n\n{code_block}\n"

    if has_rewrites:
        note += "\n## Histórico de versões\n\n"
        for i, ver in enumerate(artifact.versions):
            v_date    = ver.created_at.strftime("%Y-%m-%d %H:%M") if ver.created_at else "?"
            cmd_label = "✏️ Reescrita" if ver.command == "rewrite" else "🆕 Criação"
            note += f"### Versão {i + 1} — {cmd_label} `{v_date}`\n\n"
            note += f"```{artifact.language or 'text'}\n{ver.content}\n```\n\n"

    return note


# ── Gerador de notas — Conversas ──────────────────────────────────────────────

def format_message_block(
    msg: Message,
    artifact_map: dict,   # {art_id → Artifact}
    art_slug_map: dict,   # {art_id → "Artefatos/tipo/slug"}
    source: str = "claude",
) -> str:
    role      = msg.role
    label     = role_label(role, source)
    time_str  = msg.created_at.strftime("%H:%M") if msg.created_at else ""
    model_str = f"  `{msg.model}`" if msg.model else ""

    if role in ("human", "user", "request"):
        header = f"#### 🧑 {label}" + (f"  `{time_str}`" if time_str else "")
    elif role in ("assistant", "ai", "response"):
        icon = "🟠" if source == "deepseek" else ("🟢" if source == "chatgpt" else "🤖")
        header = f"#### {icon} {label}" + (f"  `{time_str}`" if time_str else "") + model_str
    elif role == "tool":
        header = f"#### 🔧 Tool" + (f"  `{time_str}`" if time_str else "")
    else:
        header = f"#### ⚙️ {label}"

    lines = [header, ""]

    if msg.think_text:
        lines.append("> [!note]- 🧠 Raciocínio interno *(clique para expandir)*")
        for line in msg.think_text.splitlines():
            lines.append(f"> {line}")
        lines.append("")

    if msg.text:
        lines.append(msg.text)
        lines.append("")

    if msg.has_artifacts:
        lines.append("> [!example] Artefatos gerados")
        for art_id in msg.artifacts:
            art = artifact_map.get(art_id)
            if art:
                atype, emoji = type_label(art)
                # Usa sempre o slug já resolvido — nunca recalcula
                link = art_slug_map.get(art_id, f"Artefatos/{atype}/{slugify(art.title)}")
                rewrite_note = f" *(reescrito {len(art.versions)-1}x)*" if len(art.versions) > 1 else ""
                lines.append(f"> - {emoji} [[{link}|{art.title}]]{rewrite_note}")
            else:
                lines.append(f"> - 📎 `{art_id}`")
        lines.append("")

    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def conversation_to_full_note(
    meta: ConversationMeta,
    art_slug_map: dict,   # {art_id → "Artefatos/tipo/slug"}
) -> str:
    date_str        = meta.created_at.strftime("%Y-%m-%d") if meta.created_at else "sem-data"
    time_str        = meta.created_at.strftime("%Y-%m-%dT%H:%M:%S") if meta.created_at else ""
    count_artifacts = len(meta.artifacts)
    count_messages  = len(meta.messages)
    count_branches  = len(meta.branches)
    source_label    = {"claude": "Claude", "deepseek": "DeepSeek", "chatgpt": "ChatGPT"}.get(meta.source, meta.source)
    source_icon     = {"claude": "🟤", "deepseek": "🟠", "chatgpt": "🟢"}.get(meta.source, "💬")
    source_tag      = f"{meta.source}-conversa"
    artifact_map    = {art.id: art for art in meta.artifacts}

    note = f"""---
title: "{meta.title.replace('"', "'")}"
conversation_id: {meta.id}
source: {meta.source}
created: {time_str}
artifact_count: {count_artifacts}
message_count: {count_messages}
branch_count: {count_branches}
tags:
  - {source_tag}
---

# {source_icon} {meta.title}

> **Fonte:** {source_label} · **Data:** {date_str} · **Mensagens:** {count_messages} · **Artefatos:** {count_artifacts}{f" · **Ramificações:** {count_branches}" if count_branches else ""}

"""

    if meta.artifacts:
        note += "## Artefatos\n\n"
        for art in meta.artifacts:
            atype, emoji = type_label(art)
            link = art_slug_map.get(art.id, f"Artefatos/{atype}/{slugify(art.title)}")
            rewrite_note = f" *(reescrito {len(art.versions)-1}x)*" if len(art.versions) > 1 else ""
            note += f"- {emoji} [[{link}|{art.title}]]{rewrite_note}\n"
        note += "\n"

    if meta.messages:
        note += "## Conversa\n\n"
        for msg in meta.messages:
            note += format_message_block(msg, artifact_map, art_slug_map, source=meta.source)
    else:
        note += "_Nenhuma mensagem encontrada nesta conversa._\n"

    # Ramificações no final — colapsáveis por padrão
    if meta.branches:
        note += "\n---\n\n## 🔀 Ramificações\n\n"
        note += f"> *{count_branches} caminho(s) alternativo(s) — clique para expandir*\n\n"
        for branch in meta.branches:
            preview = branch.fork_preview[:60] + "…" if len(branch.fork_preview) > 60 else branch.fork_preview
            preview_str = f": *\"{preview}\"*" if preview else ""
            note += f"> [!note]- 🔀 Ramificação {branch.branch_num} (após msg {branch.fork_index + 1}){preview_str}\n"
            for msg in branch.messages:
                role  = msg.role
                label = role_label(role, meta.source)
                if role in ("human", "user", "request"):
                    header = f"🧑 **{label}**"
                elif role in ("assistant", "ai", "response"):
                    icon   = "🟠" if meta.source == "deepseek" else ("🟢" if meta.source == "chatgpt" else "🤖")
                    a_label = {"claude": "Claude", "deepseek": "DeepSeek", "chatgpt": "ChatGPT"}.get(meta.source, label)
                    header = f"{icon} **{a_label}**"
                else:
                    header = f"⚙️ **{label}**"
                note += f"> \n> #### {header}\n"
                if msg.think_text:
                    note += f"> *[raciocínio interno omitido]*\n"
                if msg.text:
                    for line in msg.text.splitlines():
                        note += f"> {line}\n"
                note += ">\n"
            note += "\n"

    return note


# ── Persistência de índices ────────────────────────────────────────────────────

def load_index(vault_path: Path, filename: str) -> dict:
    index_file = vault_path / filename
    if not index_file.exists():
        return {}
    try:
        data = json.loads(index_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Índice não é um objeto JSON.")
        return data
    except (json.JSONDecodeError, ValueError) as e:
        backup = index_file.with_suffix(".bak")
        index_file.rename(backup)
        log.warning("Índice '%s' corrompido (%s). Backup em '%s'. Recriando.", filename, e, backup.name)
        return {}


def save_index(vault_path: Path, filename: str, data: dict):
    vault_path.mkdir(parents=True, exist_ok=True)
    (vault_path / filename).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── MOC ────────────────────────────────────────────────────────────────────────

def generate_moc(seen_convs: dict, seen_artifacts: dict, vault_path: Path, dry_run: bool):
    all_claude   = {k: v for k, v in seen_convs.items()
                    if isinstance(v, dict) and v.get("source") == "claude"}
    all_deepseek = {k: v for k, v in seen_convs.items()
                    if isinstance(v, dict) and v.get("source") == "deepseek"}
    all_chatgpt  = {k: v for k, v in seen_convs.items()
                    if isinstance(v, dict) and v.get("source") == "chatgpt"}

    moc_lines = [
        "---\ntags:\n  - MOC\n  - claude-artifacts\n  - deepseek\n  - chatgpt\n---\n",
        "# 🗺️ Conversas AI — MOC\n",
        f"> Gerado em {datetime.now().strftime('%Y-%m-%d %H:%M')} · "
        f"**{len(seen_artifacts)}** artefatos · **{len(seen_convs)}** conversas\n",
    ]

    def render_section(convs_dict: dict, label: str, icon: str):
        if not convs_dict:
            return
        by_month: dict[str, list] = defaultdict(list)
        for c in sorted(convs_dict.values(), key=lambda x: x.get("date", ""), reverse=True):
            month = c.get("month") or c.get("date", "sem-data")[:7]
            by_month[month].append(c)
        moc_lines.append(f"\n## {icon} {label}  ({len(convs_dict)} conversas)\n")
        for month in sorted(by_month.keys(), reverse=True):
            moc_lines.append(f"\n### 📅 {month}\n")
            for c in by_month[month]:
                think_tag  = " 🧠" if c.get("has_think") else ""
                fork_tag   = " 🔀" if c.get("branch_count") else ""
                art_info   = f" · {c['artifact_count']} artefatos" if c.get("artifact_count") else ""
                branch_info = f" · {c['branch_count']} ramif." if c.get("branch_count") else ""
                moc_lines.append(
                    f"- [[{c['path']}|{c['title']}]]{think_tag}{fork_tag}"
                    f"  `{c['date']}` · {c['message_count']} msg{art_info}{branch_info}"
                )

    render_section(all_claude,   "Claude",   "🟤")
    render_section(all_deepseek, "DeepSeek", "🟠")
    render_section(all_chatgpt,  "ChatGPT",  "🟢")

    type_counts: dict[str, int] = defaultdict(int)
    for path_str in seen_artifacts.values():
        parts = Path(path_str).parts
        if len(parts) >= 2 and parts[0] == "Artefatos":
            type_counts[parts[1]] += 1

    if type_counts:
        moc_lines.append("\n## Artefatos por tipo\n")
        for _, (folder, emoji) in ARTIFACT_TYPES.items():
            if type_counts[folder]:
                moc_lines.append(
                    f"- {emoji} **{folder.capitalize()}** — {type_counts[folder]} → `Artefatos/{folder}/`"
                )

    content  = "\n".join(moc_lines)
    moc_path = vault_path / "Claude + DeepSeek + ChatGPT MOC.md"
    if dry_run:
        log.info("[DRY] %s", moc_path)
    else:
        moc_path.parent.mkdir(parents=True, exist_ok=True)
        moc_path.write_text(content, encoding="utf-8")



def conv_fingerprint(meta: ConversationMeta) -> str:
    """Hash estável que representa o estado atual de uma conversa.
    Muda quando há novas mensagens, novos artefatos ou conteúdo alterado.
    """
    key = f"{len(meta.messages)}:{len(meta.artifacts)}"
    if meta.messages:
        last = meta.messages[-1]
        key += f":{last.created_at.isoformat() if last.created_at else last.text[:40]}"
    key += "".join(a.id + str(len(a.versions)) for a in meta.artifacts)
    return hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()[:16]

# ── Orquestrador principal ─────────────────────────────────────────────────────

def write_vault(
    conversations: list[ConversationMeta],
    vault_path: Path,
    dry_run: bool = False,
    incremental: bool = True,
) -> dict:
    stats: dict = defaultdict(int)

    seen_artifacts = {} if dry_run else load_index(vault_path, INDEX_FILE)
    seen_convs     = {} if dry_run else load_index(vault_path, CONV_INDEX_FILE)

    def write(path: Path, content: str):
        if dry_run:
            log.info("[DRY] %s", path)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    # 1. Resolve todos os slugs de uma vez — fonte única da verdade
    conv_slug_map = resolve_conv_slugs(conversations, seen_convs)
    art_slug_map  = resolve_artifact_slugs(
        conversations, seen_artifacts, incremental, vault_path, dry_run
    )

    # 2. Escreve artefatos + conversas
    SOURCE_FOLDER = {"claude": "Claude", "deepseek": "DeepSeek", "chatgpt": "ChatGPT"}
    for meta in conversations:
        source_folder = SOURCE_FOLDER.get(meta.source, meta.source.capitalize())
        conv_path     = f"Conversas/{source_folder}/{conv_slug_map[meta.id]}"

        # ── Detecção de mudanças ──────────────────────────────────────────────
        fingerprint   = conv_fingerprint(meta)
        existing      = seen_convs.get(meta.id)
        already_seen  = isinstance(existing, dict) and existing.get("fingerprint")
        has_changed   = not already_seen or existing.get("fingerprint") != fingerprint

        if incremental and not has_changed:
            # Conversa não mudou — pula escrita mas atualiza artefatos novos
            for art in meta.artifacts:
                if art.id not in seen_artifacts:
                    link      = art_slug_map[art.id]
                    note_file = vault_path / f"{link}.md"
                    write(note_file, artifact_to_note(art, conv_path=conv_path, conv_title=meta.title))
                    seen_artifacts[art.id] = f"{link}.md"
                    atype, _ = type_label(art)
                    stats[atype] += 1
                    stats["total_artefatos"] += 1
            stats["conversas_puladas"] += 1
            continue

        # Conversa nova ou atualizada — escreve tudo
        for art in meta.artifacts:
            link      = art_slug_map[art.id]
            note_file = vault_path / f"{link}.md"
            write(note_file, artifact_to_note(art, conv_path=conv_path, conv_title=meta.title))
            seen_artifacts[art.id] = f"{link}.md"
            atype, _ = type_label(art)
            stats[atype] += 1
            stats["total_artefatos"] += 1

        seen_convs[meta.id] = {
            "path":          conv_path,
            "title":         meta.title,
            "source":        meta.source,
            "date":          meta.created_at.strftime("%Y-%m-%d") if meta.created_at else "",
            "month":         meta.created_at.strftime("%Y-%m")    if meta.created_at else "",
            "message_count": len(meta.messages),
            "artifact_count":len(meta.artifacts),
            "has_think":     any(m.think_text for m in meta.messages),
            "branch_count":  len(meta.branches),
            "fingerprint":   fingerprint,
        }
        conv_file = vault_path / f"{conv_path}.md"
        write(conv_file, conversation_to_full_note(meta, art_slug_map))

        if already_seen and has_changed:
            log.info("🔄 Conversa atualizada: '%s'", meta.title)
            stats["conversas_atualizadas"] += 1
        else:
            stats["conversas_novas"] += 1

        stats["conversas"] += 1
        stats[f"conversas_{meta.source}"] += 1

    # 4. Persiste índices
    if not dry_run:
        save_index(vault_path, INDEX_FILE, seen_artifacts)
        save_index(vault_path, CONV_INDEX_FILE, seen_convs)

    # 5. MOC
    generate_moc(seen_convs, seen_artifacts, vault_path, dry_run)

    return stats


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Extrai conversas do Claude, DeepSeek e ChatGPT para o Obsidian.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  python claude_scraper.py export_claude.json
  python claude_scraper.py export_deepseek.json --vault ~/Documents/MeuVault
  python claude_scraper.py /pasta/export_chatgpt/   # pasta do export ChatGPT
  python claude_scraper.py export.json --dry-run
  python claude_scraper.py export.json --stats-only
  python claude_scraper.py export.json --no-incremental

Combinar todos no mesmo vault:
  python claude_scraper.py export_claude.json        --vault ~/MeuVault
  python claude_scraper.py export_deepseek.json      --vault ~/MeuVault
  python claude_scraper.py /pasta/export_chatgpt/    --vault ~/MeuVault

Como obter os exports:
  Claude   → claude.ai → Settings → Privacy & Data → Export data
  DeepSeek → chat.deepseek.com → Perfil → Export data
  ChatGPT  → chatgpt.com → Settings → Data Controls → Export data (descompactar ZIP)
        """)

    ap.add_argument("export", type=Path)
    ap.add_argument("--vault", "-v", type=Path, default=Path("./ObsidianVault"))
    ap.add_argument("--dry-run",  "-n", action="store_true")
    ap.add_argument("--stats-only", "-s", action="store_true")
    ap.add_argument("--no-incremental", action="store_true")
    ap.add_argument("--verbose", "-V", action="store_true", help="Exibe mensagens DEBUG")
    ap.add_argument("--quiet",   "-q", action="store_true", help="Exibe apenas WARNING e acima")
    args = ap.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    elif args.quiet:
        logging.getLogger().setLevel(logging.WARNING)

    if not args.export.exists():
        log.error("Arquivo não encontrado: %s", args.export)
        sys.exit(1)

    log.info("📂 Lendo export: %s", args.export)
    try:
        conversations, fmt = parse_export(args.export)
    except Exception as e:
        log.error("Erro ao parsear export: %s", e)
        sys.exit(1)

    source_icon = {"deepseek": "🟠", "chatgpt": "🟢"}.get(fmt, "🟤")
    log.info("%s Formato: %s", source_icon, fmt.upper())

    total_arts = sum(len(c.artifacts) for c in conversations)
    total_msgs = sum(len(c.messages)  for c in conversations)
    has_think  = sum(1 for c in conversations for m in c.messages if m.think_text)
    rewrites   = sum(1 for c in conversations for a in c.artifacts if len(a.versions) > 1)

    log.info("✅ %d conversas, 💬 %d mensagens", len(conversations), total_msgs)
    if fmt == "deepseek" and has_think:
        log.info("🧠 %d blocos de raciocínio", has_think)
    if fmt in ("claude", "chatgpt"):
        log.info("🎯 %d artefatos (%d reescritos)", total_arts, rewrites)

    if args.stats_only:
        if fmt == "deepseek":
            models: dict = defaultdict(int)
            for conv in conversations:
                for msg in conv.messages:
                    if msg.model:
                        models[msg.model] += 1
            print("Mensagens por modelo:")
            for m, n in sorted(models.items(), key=lambda x: -x[1]):
                print(f"  {n:5d}  {m}")
        else:
            by_type: dict = defaultdict(int)
            for conv in conversations:
                for art in conv.artifacts:
                    atype, _ = type_label(art)
                    by_type[atype] += 1
            print("Artefatos por tipo:")
            for t, n in sorted(by_type.items(), key=lambda x: -x[1]):
                print(f"  {n:4d}  {t}")
        return

    if total_msgs == 0:
        log.warning("Nenhuma mensagem encontrada. Verifique o formato do export.")
        return

    if args.dry_run:
        log.info("🔍 Dry-run — nenhum arquivo será criado")

    stats = write_vault(
        conversations, args.vault,
        dry_run=args.dry_run,
        incremental=not args.no_incremental,
    )

    if not args.dry_run:
        print(f"\n✅ Vault: {args.vault.resolve()}")
        print(f"\n📊 Resumo:")
        def conv_summary(source_key, label):
            n = stats[f"conversas_{source_key}"]
            if not n:
                return
            novas      = stats["conversas_novas"]
            atualizadas = stats["conversas_atualizadas"]
            puladas    = stats["conversas_puladas"]
            parts = []
            if atualizadas:
                parts.append(f"🔄 {atualizadas} atualizadas")
            if novas:
                parts.append(f"✨ {novas} novas")
            if puladas:
                parts.append(f"⏭ {puladas} sem mudança")
            detail = f" ({', '.join(parts)})" if parts else ""
            print(f"   {label:<9}: {n} conversas{detail}")

        conv_summary("claude",   "Claude")
        conv_summary("deepseek", "DeepSeek")
        conv_summary("chatgpt",  "ChatGPT")
        if stats["total_artefatos"]:
            rewrite_str = f" ({rewrites} com histórico)" if rewrites else ""
            print(f"   Artefatos: {stats['total_artefatos']}{rewrite_str}")
        print(f"\n   {args.vault}/")
        print(f"   ├── Claude + DeepSeek + ChatGPT MOC.md")
        print(f"   ├── Conversas/")
        if stats["conversas_claude"]:
            print(f"   │   ├── Claude/")
        if stats["conversas_deepseek"]:
            print(f"   │   ├── DeepSeek/")
        if stats["conversas_chatgpt"]:
            print(f"   │   └── ChatGPT/")
        if stats["total_artefatos"]:
            print(f"   └── Artefatos/")
            for _, (folder, emoji) in ARTIFACT_TYPES.items():
                if stats[folder]:
                    print(f"       ├── {folder}/  ({stats[folder]})")
        print(f"\n💡 Abra '{args.vault.resolve()}' no Obsidian.")


if __name__ == "__main__":
    main()