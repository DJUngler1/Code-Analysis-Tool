# Code-Analysis-Tool
Code analysis tool using locally hostesd LLMs as evaluators. The Scheduler for the Tool is writen in python

The tool itself ist deployed in docker using the docker compose file in /docker and uses a variety of static tools together with llms running in Ollama you can see the structure and what technoligies are used in this flow chart:

![Tool workflow](docs/toolflow_en.png)

## Running the tool

Requirements: Docker (Docker Desktop on macOS/Windows) and, for the LLM evaluation, [Ollama](https://ollama.com) running on the host.

1. Configure the deployment (once):
   ```sh
   cp docker/.env.example docker/.env
   ```
   Set `PROJECTS_DIR` in `docker/.env` to the folder that contains your projects. It is mounted **read-write** at the same path inside the container, so you can paste host paths unchanged. Only projects below this folder can be analyzed.
2. Build and start the container (from the repository root):
   ```sh
   docker compose -f docker/docker-compose.yml up -d --build
   ```
   The container starts and waits; nothing is analyzed yet.
3. Run the tool, from any terminal and as often as you like:
   ```sh
   bin/code-analysis
   ```
   It asks for the project path and runs the analysis inside the container. The container keeps running afterwards for the next run. To skip the prompt, pass the path: `bin/code-analysis --path /path/to/project`.

   To call it from anywhere as `code-analysis`, link the script into a folder on your `PATH`, e.g. `ln -s "$PWD/bin/code-analysis" ~/.local/bin/code-analysis`. Without the script (e.g. on Windows): `docker exec -it code-analysis-scheduler code-analysis`.
4. Stop the container when you are done:
   ```sh
   docker compose -f docker/docker-compose.yml down
   ```

After changes to the scheduler code, rebuild with `up -d --build` again.

### Acceptance check (step 1)

A project is accepted when:
- its primary language (largest share of source code) is one of C, C++, C#, Java, JavaScript, TypeScript, Python or Rust,
- it uses a package manager for that language, e.g. `pyproject.toml` / `requirements.txt`, `package.json`, `pom.xml` / `build.gradle`, `*.csproj`, `Cargo.toml`; for C/C++ only Conan (`conanfile.txt` / `conanfile.py`) or vcpkg (`vcpkg.json`) count,
- it is a git repository without uncommitted changes. Later steps (formatter) modify files, so every change must be reviewable and revertible with git.

| Exit code | Meaning |
|---|---|
| 0 | Project accepted |
| 1 | Invalid input or no project selected |
| 2 | Language not supported, or no source code found |
| 3 | No package manager found for the primary language |
| 4 | Not a git repository, or uncommitted changes |
| 125 | (`bin/code-analysis` only) Docker or the container is not running |

### Ollama

The container reaches Ollama at `http://host.docker.internal:11434` (change it with `OLLAMA_BASE_URL` in `docker/.env`). With Docker Desktop the default Ollama setup works. On Linux, Ollama must listen on all interfaces (`OLLAMA_HOST=0.0.0.0 ollama serve`), and `HOST_UID` / `HOST_GID` should be set in `docker/.env` so files written by the tool keep your ownership.

### Network

All containers join the bridge network `code-analysis-net`. The analysis tool containers added in later steps attach to it.

### Development

```sh
cd scheduler
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```
