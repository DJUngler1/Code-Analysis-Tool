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
   In `docker/.env`, set:
   - `PROJECTS_DIR` to the folder that contains your projects. It is mounted **read-write** at the same path inside the container, so you can paste host paths unchanged. Only projects below this folder can be analyzed.
   - `OUTPUT_DIR` to the folder for the results, outside your projects. It is created if missing.
2. Build the linter/formatter images (once, and again after changes in `docker/linters/`). This downloads the toolchains (about 5 GB):
   ```sh
   docker compose -f docker/docker-compose.yml --profile tools build
   ```
3. Build and start the container (from the repository root):
   ```sh
   docker compose -f docker/docker-compose.yml up -d --build
   ```
   The container starts and waits; nothing is analyzed yet.
4. Run the tool, from any terminal and as often as you like:
   ```sh
   bin/code-analysis
   ```
   It asks for the project path and runs the analysis inside the container. The container keeps running afterwards for the next run. To skip the prompt, pass the path: `bin/code-analysis --path /path/to/project`. To only check whether a project is accepted, without changing anything, add `--check-only`.

   To call it from anywhere as `code-analysis`, link the script into a folder on your `PATH`, e.g. `ln -s "$PWD/bin/code-analysis" ~/.local/bin/code-analysis`. Without the script (e.g. on Windows): `docker exec -it code-analysis-scheduler code-analysis`.
5. Stop the container when you are done:
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
| 0 | Project accepted (and, without `--check-only`, step 2 completed) |
| 1 | Invalid input or no project selected |
| 2 | Language not supported, or no source code found |
| 3 | No package manager found for the primary language |
| 4 | Not a git repository, or uncommitted changes |
| 5 | Step 2 incomplete: `OUTPUT_DIR`, Docker or a tool image missing, a tool failed, or a project file could not be moved back |
| 125 | (`bin/code-analysis` only) Docker or the container is not running |

### Linters and formatters (step 2)

After the project is accepted, the tool runs a fixed formatter and linter per language, as defined in [docs/linterConventions/README.md](docs/linterConventions/README.md). It covers every supported language found in the project, however small its share (e.g. a single C file in a Java/C++ project). A language is skipped in step 2 only if git knows none of its files (all of them ignored or in vendored/build folders).

- **What changes in the project:** the formatters and automatic fixes change files in the project. The changes stay **uncommitted**: review them with `git diff`, then commit or discard them. Until then, step 1 rejects the project on the next run. Configuration files of the project that the tools would otherwise read (`.editorconfig`, `biome.json`, `.clang-format-ignore`) are moved out during the run and moved back afterwards.
- **How the tools run:** each tool runs in a fresh container of its language image (`code-analysis/lint-<language>`). The scheduler starts these containers through the Docker socket. Project code that the tools execute, such as build scripts, .NET analyzers or CMake, only runs in those containers, which don't get the socket.
- **Results** for step 6 are written to `OUTPUT_DIR/<project>/<date_time>Z/` (UTC):

| Path | Content |
|---|---|
| `run.json` | Project, commit, start time, result of step 1 |
| `step2-linting/summary.json` | One entry per tool: version, commands, exit codes, status (`ok`, `partial`, `failed`, `skipped`), duration, changed files, number of findings, notes |
| `step2-linting/findings.json` | Findings of all tools in one format: tool, rule, severity, message, file, line, column |
| `step2-linting/changes.patch` | All changes the tools made to the project |
| `step2-linting/<language>/<tool>/` | Raw output of each tool (SARIF reports, stdout/stderr) and the tool's own `changes.patch` |
| `step2-linting/cpp-build/` | C/C++ only: the CMake build folder generated for `compile_commands.json` |

**Docker socket on Linux:** the scheduler needs the group of `/var/run/docker.sock` to start containers. The default (991) fits Docker Desktop. On Linux, set `DOCKER_SOCKET_GID` in `docker/.env` to the output of `stat -c %g /var/run/docker.sock`; the tool prints the right value if it is wrong.

### Ollama

The container reaches Ollama at `http://host.docker.internal:11434` (change it with `OLLAMA_BASE_URL` in `docker/.env`). With Docker Desktop the default Ollama setup works. On Linux, Ollama must listen on all interfaces (`OLLAMA_HOST=0.0.0.0 ollama serve`), and `HOST_UID` / `HOST_GID` should be set in `docker/.env` so files written by the tool keep your ownership.

### Network

All containers join the bridge network `code-analysis-net`, including the linter/formatter containers of step 2 (they download dependencies, e.g. crates or NuGet packages). The analysis tool containers added in later steps attach to it too.

### Development

```sh
cd scheduler
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

The integration tests of step 2 run the real tool images and are skipped by default. They need a folder that Docker can mount; on macOS the temporary folder is not shared, so use one below your home folder:

```sh
CODE_ANALYSIS_DOCKER_TEST_DIR=~/code-analysis-results/_itest .venv/bin/pytest -m docker
```
