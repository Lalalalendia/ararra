"""Fail-closed structural boundaries for the standalone Chaptera translator.

Inspired by rar2's source-ownership/monolith ratchets. This policy deliberately
inspects the *entire* repository on every CI run, not only changed files.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = Path('src/chaptera_server')
CORE = Path('src/chaptera_translation_core')
ROUTER_DIR = SERVER / 'http' / 'routers'
HTTP_METHODS = {'get', 'post', 'put', 'patch', 'delete', 'options', 'head', 'api_route', 'websocket'}
EXPECTED_ROUTERS = {'books.py', 'jobs.py', 'imports.py', 'health.py', 'identity.py', 'ui.py', 'editorial.py'}
FILE_CEILINGS = {
    SERVER / 'api.py': 90,
    SERVER / 'http' / 'schemas.py': 200,
    SERVER / 'http' / 'dependencies.py': 160,
}
ROUTER_LIMIT = 240
WEB_FILE_LIMIT = 300


def module_imports(tree: ast.AST) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            # Relative imports in server modules are checked against AST symbols separately.
            imports.add(node.module)
    return imports


def endpoint_decorators(tree: ast.AST) -> list[str]:
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call):
                decorator = decorator.func
            if isinstance(decorator, ast.Attribute) and decorator.attr in HTTP_METHODS:
                offenders.append(f'{node.name}:{decorator.attr}')
    return offenders


def scan_python(path: Path, source: str, *, path_relative: Path) -> list[str]:
    errors: list[str] = []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [f'{path_relative}: invalid Python: {exc}']

    imports = module_imports(tree)
    if path_relative.is_relative_to(CORE):
        forbidden = [name for name in imports if name == 'chaptera_server' or name.startswith('chaptera_server.') or name.split('.')[0] in {'fastapi', 'starlette', 'PyQt6', 'PySide6'}]
        if forbidden:
            errors.append(f'{path_relative}: translation core imports application/web framework: {sorted(forbidden)}')

    if path_relative.is_relative_to(SERVER) and not path_relative.is_relative_to(SERVER / 'http') and path_relative != SERVER / 'api.py':
        forbidden = [name for name in imports if name.split('.')[0] in {'fastapi', 'starlette', 'PyQt6', 'PySide6'} or name == 'chaptera_server.http' or name.startswith('chaptera_server.http.')]
        if forbidden:
            errors.append(f'{path_relative}: business/worker code imports HTTP/UI layer: {sorted(forbidden)}')

    endpoints = endpoint_decorators(tree)
    if endpoints and not path_relative.is_relative_to(ROUTER_DIR):
        errors.append(f'{path_relative}: HTTP endpoint decorators belong only to http/routers/: {endpoints}')

    if path_relative.is_relative_to(SERVER):
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == 'APIRouter' and not path_relative.is_relative_to(ROUTER_DIR):
                    errors.append(f'{path_relative}: APIRouter must be constructed inside http/routers/')
                if node.func.id == 'FastAPI' and path_relative != SERVER / 'api.py':
                    errors.append(f'{path_relative}: FastAPI app factory is owned by api.py')
    return errors


def inspect_repository(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    required = [SERVER / 'api.py', SERVER / 'http' / 'schemas.py', SERVER / 'http' / 'dependencies.py', CORE / '__init__.py', Path('web/package.json'), Path('web/src/app/App.tsx'), Path('web/src/routes/router.tsx'), Path('web/src/routes/paths.ts'), Path('web/src/shared/api/client.ts')]
    required.extend(ROUTER_DIR / name for name in sorted(EXPECTED_ROUTERS))
    for path in required:
        if not (root / path).is_file():
            errors.append(f'missing architecture-owned file: {path}')

    routers = root / ROUTER_DIR
    if routers.exists():
        found = {file.name for file in routers.glob('*.py') if file.name != '__init__.py'}
        if not EXPECTED_ROUTERS.issubset(found):
            errors.append(f'missing required HTTP routers: {sorted(EXPECTED_ROUTERS - found)}')

    for file in sorted((root / 'src').rglob('*.py')):
        if any(part in {'__pycache__', 'build', 'dist'} for part in file.parts):
            continue
        rel = file.relative_to(root)
        source = file.read_text(encoding='utf-8')
        errors.extend(scan_python(file, source, path_relative=rel))
        ceiling = FILE_CEILINGS.get(rel)
        if rel.is_relative_to(ROUTER_DIR):
            ceiling = ROUTER_LIMIT
        if ceiling is not None and len(source.splitlines()) > ceiling:
            errors.append(f'{rel}: {len(source.splitlines())} lines exceeds architecture ceiling {ceiling}')

    app_entry = root / SERVER / 'api.py'
    if app_entry.exists():
        source = app_entry.read_text(encoding='utf-8')
        if 'include_router(' not in source:
            errors.append('api.py must only compose routers, not implement endpoints')
        # A router can be renamed, but the app must continue to expose all four ownership groups.
        for name in EXPECTED_ROUTERS:
            call = f'app.include_router({name.removesuffix(".py")}.router)'
            if source.count(call) != 1:
                errors.append(f'api.py must compose {name} router exactly once')

    for file in sorted((root / 'web/src').rglob('*')):
        if not file.is_file() or file.suffix not in {'.ts', '.tsx', '.js', '.jsx'}:
            continue
        rel = file.relative_to(root)
        source = file.read_text(encoding='utf-8')
        if len(source.splitlines()) > WEB_FILE_LIMIT:
            errors.append(f'{rel}: web source exceeds {WEB_FILE_LIMIT} lines; split by feature')
        if "chaptera_server" in source or "chaptera_translation_core" in source:
            errors.append(f'{rel}: browser code must not import the Python backend or translation core')
    return errors


def main() -> int:
    errors = inspect_repository()
    if errors:
        for error in errors:
            print(f'ARCHITECTURE VIOLATION: {error}')
        return 1
    print('architecture boundaries: PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
