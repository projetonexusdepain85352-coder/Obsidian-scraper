# Claude, ChatGPT e DeepSeek to Obsidian

> ⚠️ **Projeto vibecodado, feito no meu tempo livre.**
> Funciona no meu setup — pode não funcionar no seu. Sem garantias, sem suporte formal, sujeito a bugs, quebras inesperadas e decisões questionáveis de design. Use por sua conta e risco.

Converte exports do **Claude**, **ChatGPT** e **DeepSeek** em notas Markdown prontas para o [Obsidian](https://obsidian.md/).

---

## O que faz

- Lê os arquivos de export de cada plataforma e gera notas `.md` organizadas no vault
- Suporte a **Claude** (artefatos, histórico de versões, `create_file`, widgets)
- Suporte a **ChatGPT** (export paginado `conversations-000.json`)
- Suporte a **DeepSeek** (bifurcações, blocos de raciocínio `<think>`)
- Gera um **MOC** (Map of Content) central com todas as conversas organizadas por mês
- Artefatos de código, HTML, React, SVG e Markdown salvos em pastas separadas
- Modo **incremental**: só reprocessa conversas que mudaram desde o último run (detecção por fingerprint)
- Títulos ausentes resolvidos automaticamente pelo início da primeira mensagem

---

## Estrutura gerada no vault

```
MeuVaultClaude/
├── Claude + DeepSeek + ChatGPT MOC.md
├── Conversas/
│   ├── Claude/
│   ├── DeepSeek/
│   └── ChatGPT/
└── Artefatos/
    ├── code/
    ├── html/
    ├── react/
    ├── markdown/
    └── svg/
```

---

## Requisitos

- Python 3.9+
- Windows (o `.ps1` é para PowerShell — o `.py` roda em qualquer OS)
- Obsidian (obviamente)

---

## Como usar

### 1. Estrutura de pastas esperada

```
Área de Trabalho/
└── Obsidian/
    ├── Claude Data/          ← cole aqui os exports
    │   ├── data-xxxx-batch-0000/
    │   │   └── conversations.json
    │   ├── chatgpt/
    │   │   └── conversations-000.json
    │   └── deepseek_data-2026-04-25/
    │       └── conversations.json
    └── claude_to_obsidian/   ← este repositório
        ├── claude_scraper.py
        └── scraper.ps1
```

### 2. Baixar o export

**Claude:** `claude.ai → Settings → Privacy → Export Data`

**ChatGPT:** `chatgpt.com → Settings → Data Controls → Export`

**DeepSeek:** `chat.deepseek.com → Settings → Export Data`

### 3. Rodar

**Windows (recomendado):**
```powershell
.\scraper.ps1
```

Com vault customizado:
```powershell
.\scraper.ps1 -Vault "D:\MeuVault"
```

> ⚠️ **O `scraper.ps1` foi escrito para o meu ambiente específico.**
> Ele assume que a pasta `Claude Data` fica dentro de uma estrutura no Desktop/OneDrive com nomes em português (`Área de Trabalho`, `Obsidian`, etc.).
> Se o seu setup for diferente, abra o arquivo e ajuste os caminhos nas primeiras linhas — está comentado o suficiente para ser adaptado.
> Em caso de dúvida, use o Python diretamente (veja abaixo).

**Qualquer OS (direto pelo Python):**
```bash
python claude_scraper.py caminho/para/conversations.json --vault caminho/para/vault
```

**Flags úteis:**
```
--verbose / -V     Mostra mensagens de debug
--quiet   / -q     Mostra só erros
--dry-run / -n     Simula sem escrever nada
--stats-only / -s  Só mostra estatísticas do export
--no-incremental   Reprocessa tudo do zero
```

---

## Limitações conhecidas

- Testado apenas no meu setup (Windows 11, Python 3.12, Obsidian 1.x)
- Exports muito grandes (10k+ conversas) podem ser lentos
- O formato de export das plataformas pode mudar a qualquer momento e quebrar o parser
- Bifurcações do DeepSeek são capturadas mas a renderização no Obsidian é básica
- Artefatos com IDs repetidos entre exports diferentes compartilham o mesmo arquivo no vault (comportamento intencional)

---

## Não é

- Um plugin oficial do Obsidian
- Afiliado ao Anthropic, OpenAI ou DeepSeek
- Produção-ready
- Meu trabalho principal

---

## Licença

MIT — faz o que quiser, mas não me culpe se quebrar alguma coisa.
