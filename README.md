# ARKOS

ARK (Automated Resource Knowledgebase) revolutionizes resource management via automation. Using advanced algorithms, it streamlines collection, organization, and access to resource data, facilitating efficient decision-making.

## Architecture

```
┌──────────────┐
│  arkos app   │  FastAPI server (port 1112)
│  /v1/chat/   │  OpenAI-compatible chat completions API
└──┬───┬───┬───┘
   │   │   │
   ▼   ▼   ▼
┌──────┐ ┌──────────┐ ┌──────────────────┐
│Postgres│ │  SGLang  │ │ Text Embeddings │
│memory  │ │  (LLM)   │ │ Inference (TEI)  │
│:5432   │ │  :30000  │ │ :8081            │
└────────┘ └──────────┘ └──────────────────┘
```

- **App** -- FastAPI agent that orchestrates state transitions, LLM calls, and tool usage
- **SGLang** -- serves Qwen 2.5-7B-Instruct for inference (requires GPU)
- **TEI** -- Hugging Face text embeddings for memory search (requires GPU)
- **Postgres** -- stores conversation history and OAuth tokens (via Supabase)

## Languages and Dependencies

The entire codebase is in Python, except for a few shell scripts. Docker is needed for quick setup.

### Core Dependencies

* **`openai>=1.61.0`** -- OpenAI Python SDK for standardizing inference engine communication and API compatibility
* **`pyyaml>=6.0.2`** -- YAML parser for configuration files (state graphs, etc.)
* **`pydantic>=2.10.6`** -- Data validation and schema definition using Python type annotations
* **`requests>=2.32.3`** -- HTTP library for making API requests to external services and tools

### Web Framework

* **`fastapi>=0.115.0`** -- Modern, fast web framework for building the API server with automatic OpenAPI documentation
* **`uvicorn>=0.32.0`** -- ASGI server for running FastAPI applications

### Database and Memory

* **`psycopg2-binary>=2.9.11`** -- PostgreSQL adapter for Python (binary distribution, no compilation required). Used for storing conversation context and long-term memory
* **`mem0ai`** -- Memory management library for vector-based memory storage and retrieval using Supabase

### Installation

Install all dependencies using:

```bash
pip install -r requirements.txt
```

For development (includes linting and test tools):

```bash
pip install -r requirements-dev.txt
```

## File Structure

* `base_module/` -- FastAPI app, auth routes, CLI interfaces
* `config_module/` -- YAML configuration loader
* `db/` -- Postgress task table intializer
* `model_module/` -- LLM inference wrapper (ArkModelLink)
* `agent_module/` -- agent orchestration and state machine runner
* `state_module/` -- state graph definitions (agent, tool, user states)
* `tool_module/` -- MCP (Model Context Protocol) tool integration
* `memory_module/` -- short-term (Postgres) and long-term (mem0) memory
* `tests/` -- pytest test suite

## CI/CD Pipeline

CI/CD is configured via GitHub Actions. All workflows live in `.github/workflows/`.

### CI (`ci.yml`) -- runs on every PR and push to `main`

| Job | What it does | Tools |
|-----|-------------|-------|
| **Lint** | Checks code style and formatting | `ruff check .` and `ruff format --check .` |
| **Test** | Runs unit tests with a Postgres service container | `pytest` with coverage |
| **Build** | Builds the Docker image (no push) | `docker build` |

The build job only runs after lint and test both pass.

### Deploy (`deploy.yml`) -- runs on push to `main`

Deploys to `ark.mit.edu` via SSH:
1. Creates a tarball of the repo
2. Uploads and extracts on the server
3. Backs up the current deployment
4. Installs dependencies and restarts the `arkos` systemd service
5. Runs a health check (pings `/health`)
6. Runs a smoke test (sends a real chat request through the full stack)
7. Rolls back automatically if any step fails
8. Cleans up old backups (keeps last 3)

**Setup required:**
1. Add `SSH_PRIVATE_KEY` as a GitHub repo secret (Settings > Secrets and variables > Actions)
2. Ensure the deploy user's public key is in `~/.ssh/authorized_keys` on `ark.mit.edu`
3. Ensure a systemd service named `arkos` exists on the server

### Monitor (`monitor.yml`) -- runs every 30 minutes

Pings the `/health` endpoint and reports per-service status (SGLang, TEI, Postgres). GitHub sends an email notification if the check fails.

**Setup required:** Add `MONITOR_URL` as a GitHub repo secret (e.g., `http://ark.mit.edu:1112`).

### Health Check Endpoint

`GET /health` returns the status of all services:

```json
{
  "status": "ok",
  "services": {
    "sglang": "running",
    "tei": "running",
    "postgres": "running"
  },
  "port": 1112
}
```

Returns `200` if all services are running, `503` if any service is down (status becomes `"degraded"`).

### Running CI Checks Locally

```bash
ruff check .                              # linting
ruff format --check .                     # formatting
pytest tests/ -v -m "not integration"     # unit tests
```

## Deployment Environment: MIT SIPB Shared Server (ark.mit.edu)

ARK OS is deployed on a **shared server** where multiple team members work simultaneously. This means:
- **Port conflicts** can occur when multiple users run the same services
- The **LLM inference server (port 30000)** is shared among all users
- You should use **unique ports** for your API server instance

### Start Inference Engine (REQUIRED FIRST)

The LLM server MUST be running before starting any ARK OS applications.

#### Check if LLM Server is Already Running

Since this is a shared server, someone else may have already started it:

```bash
# Check if port 30000 is in use
lsof -i :30000

# Or verify it's responding
curl http://localhost:30000/v1/models
```

If you see output, the LLM server is already running -- you can skip starting it.

#### Starting the LLM Server (if not running)

Before starting, check with your team to avoid conflicts.

```bash
bash model_module/run.sh
```

This starts the SGLang server on port 30000 using Docker and GPU. Wait for "server started" messages (may take 1-2 minutes on first run).

```bash
bash model_module/run_tei.sh
```

This starts the Huggingface-TEI server on port 4444 using Docker and GPU.

### Setting .env Variables

You need to create a `.env` and set `DB_URL` before starting the application:

1. Copy example env file:
   ```bash
   cp .env.example .env
   ```
2. Edit `.env`:
   ```bash
   DB_URL=postgresql://postgres:your-password@localhost:5432/postgres
   ```

### Running the Application

1. **Start the API server** (in one terminal):
   ```bash
   python base_module/app.py
   ```
   This starts the FastAPI server on the configured port, providing the `/v1/chat/completions` endpoint.

2. **Run the test interface** (in another terminal):
   ```bash
   python base_module/main_interface.py
   ```
   This provides an interactive CLI to test the agent. Type your messages and press Enter. Type `exit` or `quit` to stop.

### Docker Compose (Full Stack)

To run the entire stack (app + SGLang + TEI + Supabase) with Docker:

```bash
docker compose up -d
```

This requires an NVIDIA GPU with Docker GPU support configured.

## Contributors

| Name                  | Role           | GitHub username | Affiliation   |
| --------------------  | -------------- | --------------- | --------------|
| Nathaniel Morgan      | Project leader | nmorgan         | MIT           |
| Joshua Guo            | Frontend       | duck_master     | MIT           |
| Ilya Gulko            | Backend        | gulkily         | MIT           |
| Jack Luo              | Backend        | thejackluo      | Georgia Tech  |
| Bryce Roberts         | Backend        | BryceRoberts13  | MIT           |
| Angela Liu            | Backend        | angelaliu6      | MIT           |
| Ishaana Misra         | Backend        | ishaanam        | MIT           |
| Hudson Hilal          | Backend        | hhilal123       | MIT           |
| Calvin Baker          | Backend        | Calvinlb404     | MIT           |
| Kshitij Duraphe       | DevOps         | ksd3            | BU            |
