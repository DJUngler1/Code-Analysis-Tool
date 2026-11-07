from pathlib import Path

import pytest

from scheduler.linting.versions import (
    LanguageVersion,
    java_version,
    lowest_python,
    pmd_language_version,
    python_version,
    ruff_target_version,
)

POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  {properties}
  {build}
</project>
"""


def pom(properties: str = "", build: str = "") -> str:
    return POM.format(properties=properties, build=build)


def compiler_plugin(configuration: str) -> str:
    return (
        "<build><plugins><plugin><groupId>org.apache.maven.plugins</groupId>"
        "<artifactId>maven-compiler-plugin</artifactId>"
        f"<configuration>{configuration}</configuration></plugin></plugins></build>"
    )


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (pom("<properties><maven.compiler.release>17</maven.compiler.release></properties>"), "17"),
        (pom("<properties><maven.compiler.source>1.8</maven.compiler.source></properties>"), "1.8"),
        (
            pom("<properties><java.version>21</java.version>"
                "<maven.compiler.release>${java.version}</maven.compiler.release></properties>"),
            "21",
        ),
        (pom(build=compiler_plugin("<release>11</release>")), "11"),
        # The plugin's release wins over the source property.
        (
            pom("<properties><maven.compiler.source>1.8</maven.compiler.source></properties>",
                compiler_plugin("<release>17</release>")),
            "17",
        ),
        (pom("<properties><maven.compiler.release>${undefined}</maven.compiler.release></properties>"), None),
        (pom(), None),
    ],
)
def test_maven_versions(make_project, content, expected):
    root = make_project({"pom.xml": content})
    assert java_version(root, [Path("pom.xml")]).version == expected


def test_maven_preview_and_source(make_project):
    root = make_project({
        "pom.xml": pom(build=compiler_plugin("<release>27</release><compilerArgs><arg>--enable-preview</arg></compilerArgs>"))
    })
    found = java_version(root, [Path("pom.xml")])
    assert found == LanguageVersion("27", "pom.xml", preview=True)
    assert pmd_language_version(found) == "java-27-preview"


@pytest.mark.parametrize(
    ("name", "content", "expected"),
    [
        ("build.gradle.kts", "java { toolchain { languageVersion = JavaLanguageVersion.of(21) } }", "21"),
        ("build.gradle.kts", "tasks.withType<JavaCompile>().configureEach { options.release = 8 }", "8"),
        ("build.gradle", "compileJava { options.release.set(17) }", "17"),
        ("build.gradle", "sourceCompatibility = JavaVersion.VERSION_1_8", "1.8"),
        ("build.gradle", "java.sourceCompatibility = JavaVersion.VERSION_11", "11"),
        ("build.gradle", "sourceCompatibility = '17'", "17"),
        ("build.gradle", "dependencies { }", None),
    ],
)
def test_gradle_versions(make_project, name, content, expected):
    root = make_project({name: content})
    assert java_version(root, [Path(name)]).version == expected


def test_top_most_build_file_wins(make_project):
    root = make_project({
        "pom.xml": pom("<properties><maven.compiler.release>17</maven.compiler.release></properties>"),
        "module/pom.xml": pom("<properties><maven.compiler.release>21</maven.compiler.release></properties>"),
    })
    assert java_version(root, [Path("module/pom.xml"), Path("pom.xml")]).version == "17"


def test_pmd_version_without_version():
    assert pmd_language_version(LanguageVersion(None)) is None
    assert pmd_language_version(LanguageVersion("1.8")) == "java-1.8"


@pytest.mark.parametrize(
    ("specifier", "expected"),
    [
        (">=3.8", "3.8"),
        (">=3.9,<4", "3.9"),
        ("~=3.10", "3.10"),
        ("^3.11", "3.11"),
        (">3.7", "3.7"),
        ("==3.12.*", "3.12"),
        (">=3.8, !=3.9.1", "3.8"),
        ("<4", None),
        ("*", None),
        (">=2.7", None),
    ],
)
def test_lowest_python(specifier, expected):
    assert lowest_python(specifier) == expected


def test_python_version_from_requires_python(make_project):
    root = make_project({"pyproject.toml": '[project]\nname = "x"\nrequires-python = ">=3.8"\n'})
    assert python_version(root, [Path("pyproject.toml")]) == LanguageVersion("3.8", "pyproject.toml")


def test_python_version_from_poetry(make_project):
    root = make_project({"pyproject.toml": '[tool.poetry.dependencies]\npython = "^3.10"\n'})
    assert python_version(root, []).version == "3.10"


def test_python_version_missing(make_project):
    root = make_project({"requirements.txt": "requests\n"})
    assert python_version(root, [Path("requirements.txt")]).version is None


@pytest.mark.parametrize(
    ("version", "expected"),
    [("3.8", "py38"), ("3.12", "py312"), ("3.6", "py37"), (None, None), ("2.7", None)],
)
def test_ruff_target_version(version, expected):
    assert ruff_target_version(LanguageVersion(version)) == expected
