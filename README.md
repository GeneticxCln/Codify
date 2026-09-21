# Codify

**Codify** is a local-first, multi-agent AI coding assistant and desktop application. It decomposes high-level software engineering goals into an atomic, verified execution plan using an orchestrated pipeline of specialized subagents, sandboxed test execution, git integration, and real-time telemetry.

---

## 🏛️ Architecture

Codify follows a strict two-tier architecture:

```
┌─────────────────────────────────────────────────────────────┐
│                    Codify Desktop App                       │
│  ┌─────────────────────────┐   ┌─────────────────────────┐  │
│  │  React 19 + TypeScript  │◄──┤      Tauri v2 Core      │  │
│  │   Tailwind CSS / Vite   │──►│   (Rust Subprocess Host)│  │
│  └─────────────────────────┘   └────────────┬────────────┘  │
└─────────────────────────────────────────────┼───────────────┘
                                              │ Spawns & supervises
                                              ▼ (Stdout Handshake)
┌─────────────────────────────────────────────────────────────┐
│                    Codify Engine Backend                    │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  FastAPI (127.0.0.1:7430-7440, Bearer Auth Token)     │  │
│  └──────────────────────────┬────────────────────────────┘  │
│                             │                               │
│  ┌──────────────────────────┴────────────────────────────┐  │
│  │            5-Role Subagent Orchestrator               │  │
│  │  ┌──────────┐ ┌───────┐ ┌────────┐ ┌────────┐ ┌─────┐ │  │
│  │  │ Planner  │►│ Coder │►│ Tester │►│Reviewer│►│ Sum │ │  │
│  │  └──────────┘ └───────┘ └────────┘ └────────┘ └─────┘ │  │
│  └──────┬──────────────────────┬─────────────────────────┘  │
│         ▼                      ▼                            │
│  ┌──────────────┐       ┌──────────────┐     ┌───────────┐  │
│  │ FileSystem   │       │ Sandbox      │     │ Git       │  │
│  │ Service      │       │ Service      │     │ Service   │  │
│  │ (Path Jails) │       │ (Allowlist)  │     │ (Commits) │  │
│  └──────────────┘       └──────────────┘     └───────────┘  │
│         │                      │                            │
│         ▼                      ▼                            │
│  SQLite (WAL Mode)       Local OS Keyring                   │
└─────────────────────────────────────────────────────────────┘
```

### The 5 Subagent Roles

Codify enforces a sequential, five-stage subagent pipeline for every plan step:

| Role | Responsibility | Models / Protocols |
|---|---|---|
| **Planner** | Decomposes high-level goals into 1–20 sequential, actionable steps with suggested paths. | Anthropic, OpenAI, DeepSeek, Google Gemini, Ollama |
| **Coder** | Proposes concrete file creates, updates, or deletions as unified diffs. | Anthropic, OpenAI, DeepSeek, Google Gemini, Ollama |
| **Tester** | Emits sandboxed verification commands (`pytest`, `npm test`, `cargo test`, etc.) and determines step pass/fail. | Anthropic, OpenAI, DeepSeek, Google Gemini, Ollama |
| **Reviewer** | Audits diffs for correctness, secret leaks, and security safety. Can approve or reject (`request-changes`). | Anthropic, OpenAI, DeepSeek, Google Gemini, Ollama |
| **Summarizer** | Generates human-readable progress notes and conventional git commit messages. | Anthropic, OpenAI, DeepSeek, Google Gemini, Ollama |

---

## 🔒 Security & Invariants

1. **Loopback Only**: The engine binds strictly to `127.0.0.1` on ports `7430–7440`.
2. **Ephemeral Boot Token**: On launch, the engine generates a 32-byte CSPRNG token (`CODIFY_ENGINE token=<hex> port=<int>`). All HTTP and WebSocket requests require `Authorization: Bearer <token>`.
3. **Workspace Path Containment**: All file operations verify paths with realpath containment (`FileSystemService.resolve`). Path escapes outside workspace roots raise `PathEscapeError`.
4. **Command Sandboxing**: Shell commands executed by the tester pass through `SandboxService` which strictly enforces an allowlisted binary set (`pytest`, `python`/`python3 -m pytest`, `npm test`, `pnpm test`, `cargo test`, `go test`, and read-only `git status`/`diff`/`log -1`).
5. **Human-in-the-Loop Rejection**: When the Reviewer agent requests changes, the step halts in `IN_PROGRESS` with review notes and the goal transitions to `PAUSED`. Execution resumes only when a human user reviews and explicitly triggers a retry.
6. **OS Keyring Security**: Subagent API keys are never written to SQLite or disk; they are stored in the platform's OS Keyring (`keyring` library / Linux Secret Service / macOS Keychain / Windows Credential Manager).

---

## 🚀 Quickstart

### Prerequisites

- **Python**: 3.10+ (tested up to 3.14)
- **Node.js**: 18+ (tested with Node 20 / 26)
- **Rust / Cargo**: 1.77+ (for desktop shell)

### Setup & Installation

```bash
# Clone the repository
git clone https://github.com/your-org/codify.git
cd Codify

# Install Python dependencies
pip install -r engine/requirements.txt

# Install UI dependencies
cd ui && npm install && cd ..
```

### Running All Verifications

```bash
make check
```
This executes:
1. Full Python test suite (27 unit & integration tests)
2. UI TypeScript validation and Vite production build
3. Tauri Rust crate typecheck via `cargo check`

### Launching the Application

#### Option A: Tauri Desktop App
```bash
cd src-tauri
cargo run
```

#### Option B: Standalone Engine + Vite Dev Server
```bash
# Terminal 1: Run engine
python3 -m engine

# Terminal 2: Run web frontend
make dev-ui
```

---

## 📂 Project Structure

```
Codify/
├── engine/                # FastAPI backend & orchestration engine
│   ├── app.py             # FastAPI routes, WebSocket handler & lifespan
│   ├── executor.py        # 5-stage agent execution & state transitions
│   ├── models.py          # Pydantic domain models & schemas
│   ├── providers.py       # LLM provider implementations (Anthropic, OpenAI, Gemini, Ollama)
│   ├── services.py        # Workspace, Goal, Event, and Registry services
│   ├── sandbox.py         # Command execution sandbox & argv validation
│   ├── fs.py              # Path containment & atomic file operations
│   ├── git.py             # Git repository detection, diffing, and commits
│   ├── db.py              # SQLite connection, WAL configuration, and seeders
│   └── default_prompts.py # System prompts for all 5 subagent roles
├── ui/                    # React 19 + TypeScript desktop frontend
│   ├── src/
│   │   ├── components/    # SettingsPanel, AgentConfigCard, GoalDetailView, etc.
│   │   ├── hooks/         # useAgentConfigs hook
│   │   ├── api.ts         # Tauri IPC & HTTP fallback client
│   │   └── types.ts       # TypeScript type definitions
│   └── vite.config.ts     # Vite configuration
├── src-tauri/             # Tauri v2 desktop application shell
│   ├── src/
│   │   ├── lib.rs         # Subprocess supervisor & IPC command handlers
│   │   └── main.rs        # Application entry point
│   ├── tauri.conf.json    # Tauri configuration & capabilities
│   └── Cargo.toml         # Rust dependencies
├── tests/                 # Comprehensive test suite (27 tests)
│   ├── test_api.py        # HTTP & WebSocket route integration tests
│   ├── test_executor.py   # Full multi-agent execution pipeline tests
│   ├── test_fs.py         # File system jail & diffing unit tests
│   ├── test_git.py        # Git integration tests
│   ├── test_sandbox.py    # Sandbox argv allowlist security tests
│   └── test_db_and_services.py # Database & service layer unit tests
├── docs/                  # Architecture specifications & protocols
├── Makefile               # Development, build, and test automation
└── pyproject.toml         # Python packaging configuration
```

---

## 🧪 Testing

Run the test suite with:

```bash
# Via Makefile
make test

# Or directly with Python unittest
python3 -m unittest discover -s tests -p "test_*.py" -v
```

All 27 tests run in sub-second time without external network calls using deterministic mock providers and in-memory/temporary databases.

---

## 📄 License

MIT License. See [LICENSE](LICENSE) for details.
