#!/usr/bin/env python3
"""
Check Python dependencies for outdated packages.

Supports both requirements.txt and pyproject.toml files.

Usage:
    check_requirements.py <requirements.txt>            # Display outdated packages
    check_requirements.py <pyproject.toml>              # Display outdated packages from pyproject.toml
    check_requirements.py <file> --create               # Create <file>.new with latest versions
    check_requirements.py <file> --update               # Backup original and update in place

2026/11/02  V1.0.0  Initial version
2026/11/02  V1.0.1  Add pyproject.toml support
"""

import argparse
import logging
import re
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("Error: 'requests' module is required. Install with: pip install requests", file=sys.stderr)
    sys.exit(1)

try:
    import tomllib
except ImportError:
    # Python < 3.11 fallback
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

logger = logging.getLogger(__name__)


class FileType:
    """Enum for supported file types."""
    REQUIREMENTS = "requirements"
    PYPROJECT = "pyproject"


def detect_file_type(file_path: Path) -> str:
    """Detect the type of dependency file."""
    if file_path.name == "pyproject.toml" or file_path.suffix == ".toml":
        return FileType.PYPROJECT
    return FileType.REQUIREMENTS


def parse_requirement_line(line: str) -> tuple[str, str, str] | None:
    """
    Parse a requirement line and return (package_name, version_spec, original_line).
    Returns None for comments, empty lines, or unparseable lines.
    """
    stripped = line.strip()
    
    # Skip empty lines and comments
    if not stripped or stripped.startswith('#'):
        return None
    
    # Remove inline comments
    if '#' in stripped:
        stripped = stripped.split('#')[0].strip()
    
    # Match package with version specifier
    # Supports: ==, >=, <=, >, <, ~=, !=
    match = re.match(r'^([a-zA-Z0-9_-]+(?:\[[a-zA-Z0-9_,]+\])?)\s*([><=!~]+)\s*([0-9a-zA-Z.*]+)', stripped)
    if match:
        package_name = match.group(1)
        # Remove extras like [extra1,extra2]
        if '[' in package_name:
            package_name = package_name.split('[')[0]
        version = match.group(3)
        return (package_name.lower(), version, line)
    
    # Package without version specifier
    match = re.match(r'^([a-zA-Z0-9_-]+(?:\[[a-zA-Z0-9_,]+\])?)$', stripped)
    if match:
        package_name = match.group(1)
        if '[' in package_name:
            package_name = package_name.split('[')[0]
        return (package_name.lower(), '', line)
    
    return None


def parse_pyproject_dependency(dep: str) -> tuple[str, str, str] | None:
    """
    Parse a pyproject.toml dependency string.
    Returns (package_name, version, original_string) or None.
    
    Examples:
        "requests>=2.28.0" -> ("requests", "2.28.0", "requests>=2.28.0")
        "flask[async]>=2.0" -> ("flask", "2.0", "flask[async]>=2.0")
        "numpy" -> ("numpy", "", "numpy")
    """
    dep = dep.strip()
    if not dep:
        return None
    
    # Match package with optional extras and version specifier
    # Pattern: package_name[extras]>=version or package_name[extras]==version etc.
    match = re.match(
        r'^([a-zA-Z0-9_-]+)(\[[^\]]+\])?\s*([><=!~]=?)\s*([0-9a-zA-Z.*]+)',
        dep
    )
    if match:
        package_name = match.group(1).lower()
        version = match.group(4)
        return (package_name, version, dep)
    
    # Package without version (just name, possibly with extras)
    match = re.match(r'^([a-zA-Z0-9_-]+)(\[[^\]]+\])?$', dep)
    if match:
        package_name = match.group(1).lower()
        return (package_name, '', dep)
    
    return None


def parse_pyproject_toml(file_path: Path) -> list[dict]:
    """
    Parse dependencies from a pyproject.toml file.
    Returns list of dicts with package info.
    """
    if tomllib is None:
        logger.error("TOML parsing requires Python 3.11+ or 'tomli' package. Install with: pip install tomli")
        sys.exit(1)
    
    with open(file_path, 'rb') as f:
        data = tomllib.load(f)
    
    packages = []
    
    # PEP 621 dependencies (project.dependencies)
    project = data.get('project', {})
    dependencies = project.get('dependencies', [])
    
    for dep in dependencies:
        parsed = parse_pyproject_dependency(dep)
        if parsed:
            packages.append({
                'name': parsed[0],
                'current_version': parsed[1],
                'original_line': parsed[2],
                'latest_version': None,
                'needs_update': False,
                'section': 'project.dependencies'
            })
    
    # Optional dependencies (project.optional-dependencies)
    optional_deps = project.get('optional-dependencies', {})
    for group_name, group_deps in optional_deps.items():
        for dep in group_deps:
            parsed = parse_pyproject_dependency(dep)
            if parsed:
                packages.append({
                    'name': parsed[0],
                    'current_version': parsed[1],
                    'original_line': parsed[2],
                    'latest_version': None,
                    'needs_update': False,
                    'section': f'project.optional-dependencies.{group_name}'
                })
    
    # Poetry dependencies (tool.poetry.dependencies)
    poetry = data.get('tool', {}).get('poetry', {})
    poetry_deps = poetry.get('dependencies', {})
    
    for pkg_name, version_spec in poetry_deps.items():
        if pkg_name.lower() == 'python':
            continue  # Skip Python version constraint
        
        if isinstance(version_spec, str):
            # Simple version: "^2.28.0" or ">=1.0"
            version = re.sub(r'^[\^~>=<!\s]+', '', version_spec)
        elif isinstance(version_spec, dict):
            # Complex spec: {version = "^2.28.0", optional = true}
            version = version_spec.get('version', '')
            version = re.sub(r'^[\^~>=<!\s]+', '', version)
        else:
            version = ''
        
        packages.append({
            'name': pkg_name.lower(),
            'current_version': version,
            'original_line': f'{pkg_name} = "{version_spec}"' if isinstance(version_spec, str) else str(version_spec),
            'latest_version': None,
            'needs_update': False,
            'section': 'tool.poetry.dependencies'
        })
    
    # Poetry dev dependencies (tool.poetry.dev-dependencies or tool.poetry.group.dev.dependencies)
    poetry_dev_deps = poetry.get('dev-dependencies', {})
    for pkg_name, version_spec in poetry_dev_deps.items():
        if isinstance(version_spec, str):
            version = re.sub(r'^[\^~>=<!\s]+', '', version_spec)
        elif isinstance(version_spec, dict):
            version = version_spec.get('version', '')
            version = re.sub(r'^[\^~>=<!\s]+', '', version)
        else:
            version = ''
        
        packages.append({
            'name': pkg_name.lower(),
            'current_version': version,
            'original_line': f'{pkg_name} = "{version_spec}"' if isinstance(version_spec, str) else str(version_spec),
            'latest_version': None,
            'needs_update': False,
            'section': 'tool.poetry.dev-dependencies'
        })
    
    # Poetry group dependencies (tool.poetry.group.*.dependencies)
    poetry_groups = poetry.get('group', {})
    for group_name, group_config in poetry_groups.items():
        group_deps = group_config.get('dependencies', {})
        for pkg_name, version_spec in group_deps.items():
            if isinstance(version_spec, str):
                version = re.sub(r'^[\^~>=<!\s]+', '', version_spec)
            elif isinstance(version_spec, dict):
                version = version_spec.get('version', '')
                version = re.sub(r'^[\^~>=<!\s]+', '', version)
            else:
                version = ''
            
            packages.append({
                'name': pkg_name.lower(),
                'current_version': version,
                'original_line': f'{pkg_name} = "{version_spec}"' if isinstance(version_spec, str) else str(version_spec),
                'latest_version': None,
                'needs_update': False,
                'section': f'tool.poetry.group.{group_name}.dependencies'
            })
    
    return packages


def get_latest_version(package_name: str) -> str | None:
    """Fetch the latest version of a package from PyPI."""
    url = f"https://pypi.org/pypi/{package_name}/json"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return data['info']['version']
    except Exception:
        pass
    return None


def parse_version(version: str) -> tuple:
    """Parse version string into comparable tuple."""
    # Remove leading zeros and split by dots
    parts = []
    for part in version.split('.'):
        # Handle versions like "1.0a1", "2.0rc1", "3.0.dev1"
        match = re.match(r'^(\d+)(.*)', part)
        if match:
            parts.append((int(match.group(1)), match.group(2)))
        else:
            parts.append((0, part))
    return tuple(parts)


def compare_versions(current: str, latest: str) -> bool:
    """Return True if latest > current."""
    try:
        current_parsed = parse_version(current)
        latest_parsed = parse_version(latest)
        return latest_parsed > current_parsed
    except Exception:
        return False


def parse_requirements_txt(file_path: Path) -> list[dict]:
    """
    Parse dependencies from a requirements.txt file.
    Returns list of dicts with package info.
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    packages = []
    for line in lines:
        parsed = parse_requirement_line(line)
        if parsed:
            packages.append({
                'name': parsed[0],
                'current_version': parsed[1],
                'original_line': parsed[2],
                'latest_version': None,
                'needs_update': False,
                'section': None
            })
    
    return packages


def check_dependencies(file_path: Path) -> list[dict]:
    """
    Check all dependencies for updates.
    Automatically detects file type (requirements.txt or pyproject.toml).
    Returns list of dicts with package info.
    """
    file_type = detect_file_type(file_path)
    
    if file_type == FileType.PYPROJECT:
        packages = parse_pyproject_toml(file_path)
    else:
        packages = parse_requirements_txt(file_path)
    
    if not packages:
        logger.warning("No dependencies found in %s", file_path)
        return packages
    
    # Fetch latest versions in parallel
    logger.info("Checking %d packages against PyPI...", len(packages))
    
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_pkg = {
            executor.submit(get_latest_version, pkg['name']): pkg 
            for pkg in packages
        }
        
        for future in as_completed(future_to_pkg):
            pkg = future_to_pkg[future]
            try:
                latest = future.result()
                pkg['latest_version'] = latest
                if latest and pkg['current_version']:
                    pkg['needs_update'] = compare_versions(pkg['current_version'], latest)
            except Exception:
                pass
    
    return packages


def display_results(packages: list[dict]) -> None:
    """Display packages that have updates available."""
    outdated = [p for p in packages if p['needs_update']]
    
    if not outdated:
        logger.info("All packages are up to date!")
        return
    
    header = f"\n{'Package':<25} {'Current':<15} {'Latest':<15}"
    separator = "-" * 55
    logger.info(header)
    logger.info(separator)
    
    for pkg in sorted(outdated, key=lambda x: x['name']):
        logger.info("%s %s %s", pkg['name'].ljust(25), pkg['current_version'].ljust(15), pkg['latest_version'].ljust(15))
    
    logger.info("\n%d package(s) can be updated.", len(outdated))


def update_requirements_content(file_path: Path, packages: list[dict]) -> str:
    """Generate updated requirements.txt content with latest versions."""
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    for pkg in packages:
        if pkg['needs_update'] and pkg['latest_version']:
            # Replace version in the line
            # Match the package name followed by version specifier (>=, ==, ~=, etc.)
            old_pattern = rf"({re.escape(pkg['name'])}(?:\[[^\]]+\])?)\s*([><=!~]+)\s*{re.escape(pkg['current_version'])}"
            new_value = rf"\1\2{pkg['latest_version']}"
            content = re.sub(old_pattern, new_value, content, flags=re.IGNORECASE)
    
    return content


def update_pyproject_content(file_path: Path, packages: list[dict]) -> str:
    """Generate updated pyproject.toml content with latest versions."""
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    for pkg in packages:
        if pkg['needs_update'] and pkg['latest_version']:
            current = pkg['current_version']
            latest = pkg['latest_version']
            name = pkg['name']
            
            # Handle PEP 621 style: "package>=version" or "package==version"
            # In arrays like dependencies = ["requests>=2.28.0"]
            old_pattern = rf'"{name}(\[[^\]]*\])?\s*([><=!~]+)\s*{re.escape(current)}"'
            new_value = rf'"{name}\1\2{latest}"'
            content = re.sub(old_pattern, new_value, content, flags=re.IGNORECASE)
            
            # Handle Poetry style: package = "^version" or package = ">=version"
            old_pattern = rf'({name}\s*=\s*")[\^~>=<]*{re.escape(current)}(")'
            new_value = rf'\g<1>^{latest}\2'
            content = re.sub(old_pattern, new_value, content, flags=re.IGNORECASE)
            
            # Handle Poetry complex style: package = {version = "^version", ...}
            old_pattern = rf'({name}\s*=\s*\{{[^}}]*version\s*=\s*")[\^~>=<]*{re.escape(current)}(")'
            new_value = rf'\g<1>^{latest}\2'
            content = re.sub(old_pattern, new_value, content, flags=re.IGNORECASE)
    
    return content


def update_file_content(file_path: Path, packages: list[dict]) -> str:
    """Generate updated file content with latest versions."""
    file_type = detect_file_type(file_path)
    
    if file_type == FileType.PYPROJECT:
        return update_pyproject_content(file_path, packages)
    else:
        return update_requirements_content(file_path, packages)


def create_new_file(file_path: Path, packages: list[dict]) -> Path:
    """Create a new file with updated versions in current directory."""
    content = update_file_content(file_path, packages)
    
    output_path = Path.cwd() / f"{file_path.name}.new"
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(content)
    
    return output_path


def update_file_in_place(file_path: Path, packages: list[dict]) -> tuple[Path, Path]:
    """
    Backup the original file and update it in place.
    Returns (backup_path, updated_path).
    """
    # Create backup with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{file_path.stem}.{timestamp}.bak"
    backup_path = file_path.parent / backup_name
    
    # Copy original to backup
    shutil.copy2(file_path, backup_path)
    
    # Generate updated content and write to original path
    content = update_file_content(file_path, packages)
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(content)
    
    return backup_path, file_path


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s"
    )
    
    parser = argparse.ArgumentParser(
        description="Check Python dependencies for outdated packages (supports requirements.txt and pyproject.toml)"
    )
    parser.add_argument(
        'file',
        type=Path,
        help="Path to requirements.txt or pyproject.toml file"
    )
    parser.add_argument(
        '--create',
        action='store_true',
        help="Create <file>.new with updated versions in current directory"
    )
    parser.add_argument(
        '--update',
        action='store_true',
        help="Backup original file and update it in place"
    )
    
    args = parser.parse_args()
    
    if args.create and args.update:
        logger.error("--create and --update are mutually exclusive")
        sys.exit(1)
    
    if not args.file.exists():
        logger.error("File not found: %s", args.file)
        sys.exit(1)
    
    packages = check_dependencies(args.file)
    outdated_count = sum(1 for p in packages if p['needs_update'])
    
    if args.create:
        output_path = create_new_file(args.file, packages)
        logger.info("Created %s with %d updated package(s)", output_path, outdated_count)
    elif args.update:
        if outdated_count == 0:
            logger.info("All packages are up to date, no changes needed.")
        else:
            backup_path, updated_path = update_file_in_place(args.file, packages)
            logger.info("Backup created: %s", backup_path)
            logger.info("Updated %s with %d package(s)", updated_path, outdated_count)
    else:
        display_results(packages)


if __name__ == '__main__':
    main()
