# Origin

Origin is a capability-based operating system simulation written in Python.
It is a kernel-style runtime and REPL environment for simulating a minimal OS,
not a normal end-user application.

## Project structure

```text
Origin/
├── README.md
├── pyproject.toml
├── CONSTITUTION.md
├── src/
│   └── origin/
│       ├── __init__.py
│       ├── __main__.py
│       ├── cli.py
│       ├── main.py
│       ├── capabilities/
│       │   └── __init__.py
│       ├── core/
│       │   ├── __init__.py
│       │   ├── bootstrap.py
│       │   ├── capability.py
│       │   ├── constitution.py
│       │   ├── ids.py
│       │   ├── message.py
│       │   ├── nucleus.py
│       │   ├── objects.py
│       │   └── unit.py
│       ├── human/
│       │   └── __init__.py
│       ├── kernel/
│       │   └── __init__.py
│       ├── messages/
│       │   └── __init__.py
│       ├── objects/
│       │   └── __init__.py
│       ├── services/
│       │   └── __init__.py
│       ├── simulator/
│       │   └── __init__.py
│       └── units/
│           ├── __init__.py
│           ├── console.py
│           ├── demo.py
│           ├── improver.py
│           ├── naming.py
│           ├── object_store.py
│           └── watcher.py
└── tests/
    └── test_package.py
```

## Quick start

```bash
py -m pip install -e .
py -m origin --help
```

## Run the demo

```bash
py -m origin --demo
```

## Run the REPL

```bash
py -m origin
```

## Notes

- The package root remains import-friendly for compatibility.
- The actual implementation still lives primarily under the `origin.core` package.
- The OS-style facade packages such as `origin.kernel`, `origin.capabilities`, `origin.messages`, and `origin.services` are available as a cleaner architectural boundary.
- Subpackages such as `origin.units` remain isolated and importable in the usual Python way.
