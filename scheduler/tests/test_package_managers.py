import pytest

from scheduler.acceptance.filetree import scan_project
from scheduler.acceptance.package_managers import detect_package_managers


def detect(make_project, files, language):
    root = make_project(files)
    return [(hit.manager, hit.manifest.as_posix()) for hit in detect_package_managers(root, scan_project(root), language)]


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"pyproject.toml": "[project]\nname = 'x'\n"}, [("pip", "pyproject.toml")]),
        ({"requirements-dev.txt": "pytest\n"}, [("pip", "requirements-dev.txt")]),
        ({"requirements/base.txt": "django\n"}, [("pip", "requirements/base.txt")]),
        ({"pyproject.toml": "[tool.poetry]\nname = 'x'\n"}, [("Poetry", "pyproject.toml")]),
        ({"pyproject.toml": "[project]\n", "uv.lock": ""}, [("uv", "uv.lock")]),
        ({"Pipfile": ""}, [("Pipenv", "Pipfile")]),
        ({"environment.yml": ""}, [("Conda", "environment.yml")]),
        ({"main.py": ""}, []),
    ],
)
def test_python(make_project, files, expected):
    assert detect(make_project, files, "Python") == expected


def test_python_finds_manifests_in_subfolders(make_project):
    files = {"services/api/pyproject.toml": "[project]\n", "services/worker/requirements.txt": ""}
    assert detect(make_project, files, "Python") == [
        ("pip", "services/api/pyproject.toml"),
        ("pip", "services/worker/requirements.txt"),
    ]


@pytest.mark.parametrize(
    ("lockfile", "manager"),
    [
        (None, "npm"),
        ("package-lock.json", "npm"),
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "Yarn"),
        ("bun.lock", "Bun"),
    ],
)
def test_node_manager_from_lockfile(make_project, lockfile, manager):
    files = {"package.json": "{}"}
    if lockfile:
        files[lockfile] = ""
    assert detect(make_project, files, "TypeScript") == [(manager, "package.json")]


def test_node_workspace_packages_use_root_lockfile(make_project):
    files = {"package.json": "{}", "pnpm-lock.yaml": "", "packages/ui/package.json": "{}"}
    assert detect(make_project, files, "JavaScript") == [
        ("pnpm", "package.json"),
        ("pnpm", "packages/ui/package.json"),
    ]


def test_node_modules_are_ignored(make_project):
    assert detect(make_project, {"node_modules/lib/package.json": "{}"}, "JavaScript") == []


@pytest.mark.parametrize(
    ("files", "language", "expected"),
    [
        ({"pom.xml": ""}, "Java", [("Maven", "pom.xml")]),
        ({"build.gradle.kts": "", "settings.gradle.kts": ""}, "Java", [("Gradle", "settings.gradle.kts")]),
        ({"src/App/App.csproj": ""}, "C#", [("NuGet", "src/App/App.csproj")]),
        ({"Cargo.toml": ""}, "Rust", [("Cargo", "Cargo.toml")]),
        ({"vcpkg.json": "{}"}, "C++", [("vcpkg", "vcpkg.json")]),
        ({"conanfile.py": ""}, "C", [("Conan", "conanfile.py")]),
        # Build systems alone do not count as package managers.
        ({"CMakeLists.txt": "", "Makefile": "", "meson.build": ""}, "C++", []),
    ],
)
def test_other_languages(make_project, files, language, expected):
    assert detect(make_project, files, language) == expected


def test_only_markers_of_the_requested_language_count(make_project):
    assert detect(make_project, {"package.json": "{}"}, "Python") == []
