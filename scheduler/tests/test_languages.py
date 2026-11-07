from scheduler.acceptance.filetree import scan_project
from scheduler.acceptance.languages import detect_languages


def detect(make_project, files):
    root = make_project(files)
    return detect_languages(scan_project(root), root=root)


def test_python_project(make_project):
    report = detect(make_project, {"app/main.py": "print('hi')\n", "README.md": "# docs " * 100})
    assert report.primary == "Python"
    assert report.primary_supported
    assert report.others == ()


def test_primary_is_largest_language_and_others_are_reported(make_project):
    report = detect(
        make_project,
        {
            "backend/app.py": "x" * 7000,
            "frontend/src/app.tsx": "x" * 2500,
            "scripts/tool.go": "x" * 500,
        },
    )
    assert report.primary == "Python"
    assert report.others == ("TypeScript", "Go")
    assert report.supported_others == ("TypeScript",)
    assert report.unsupported_others == ("Go",)
    assert report.share("Python") == 0.7


def test_every_language_counts_however_small_its_share(make_project):
    # libgdx: one C file next to a large Java and C++ code base.
    report = detect(make_project, {"A.java": "x" * 65_000, "b.cpp": "x" * 34_000, "c.c": "x" * 130})
    assert report.primary == "Java"
    assert report.supported_others == ("C++", "C")
    assert report.share("C") < 0.01


def test_xml_files_with_typescript_extensions_are_not_typescript(make_project):
    report = detect(
        make_project,
        {
            "App.java": "x" * 1000,
            # Tileset of the Tiled map editor and a Qt translation file.
            "assets/tiles.tsx": '<?xml version="1.0" encoding="UTF-8"?>\n<tileset name="tiles"/>\n',
            "i18n/app_de.ts": "<!DOCTYPE TS>\n<TS version=\"2.1\" language=\"de\"></TS>\n",
            "web/real.tsx": "export const App = () => <div />;\n",
        },
    )
    assert report.bytes_by_language == {"Java": 1000, "TypeScript": len("export const App = () => <div />;\n")}


def test_unsupported_language_is_detected_as_primary(make_project):
    report = detect(make_project, {"main.go": "package main\n", "go.mod": "module x\n"})
    assert report.primary == "Go"
    assert not report.primary_supported


def test_significant_unsupported_secondary_language(make_project):
    report = detect(make_project, {"app.py": "x" * 800, "tool.go": "x" * 200})
    assert report.unsupported_others == ("Go",)


def test_no_source_code(make_project):
    report = detect(make_project, {"README.md": "# hello\n", "run.sh": "echo hi\n"})
    assert report.primary is None
    assert not report.primary_supported


def test_empty_files_do_not_count(make_project):
    assert detect(make_project, {"pkg/__init__.py": ""}).primary is None


def test_headers_belong_to_cpp_when_cpp_sources_exist(make_project):
    report = detect(make_project, {"src/a.h": "x" * 1000, "src/a.cpp": "x" * 100, "src/b.c": "x" * 500})
    assert report.primary == "C++"


def test_headers_belong_to_c_by_default(make_project):
    report = detect(make_project, {"src/a.h": "x" * 1000, "src/a.c": "x" * 100})
    assert report.primary == "C"
    assert report.bytes_by_language == {"C": 1100}


def test_dependencies_build_output_and_generated_files_are_ignored(make_project):
    report = detect(
        make_project,
        {
            "src/index.ts": "x" * 100,
            "node_modules/lib/index.js": "x" * 10_000,
            "dist/index.js": "x" * 10_000,
            ".cache/x.js": "x" * 10_000,
            "public/vendor.min.js": "x" * 10_000,
            "types/api.d.ts": "x" * 10_000,
            "myenv/pyvenv.cfg": "home = /usr/bin\n",
            "myenv/lib/site.py": "x" * 10_000,
        },
    )
    assert report.bytes_by_language == {"TypeScript": 100}
