# Changelog

All notable changes to this project are documented in this file.

## [1.0.1] - 2026-11-02

### Added

#### check_requirements.py
- Support for `pyproject.toml` files (PEP 621 and Poetry formats)
- Parse PEP 621 `project.dependencies` and `project.optional-dependencies`
- Parse Poetry `tool.poetry.dependencies`, `tool.poetry.dev-dependencies`, and `tool.poetry.group.*.dependencies`
- Automatic file type detection based on filename
- Use Python 3.11+ built-in `tomllib` or fallback to `tomli` package

## [1.0.0] - 2026-11-02

### Added

#### check_requirements.py
- Check Python packages against PyPI
- Parse `requirements.txt` files (comments, extras `[...]`, versions)
- Parallel requests to PyPI (10 threads)
- Intelligent version comparison (support for alpha, beta, rc, dev suffixes)
- `--create` option: generates `<file>.new` in the current directory
- `--update` option: backup with timestamp and in-place update

#### check_docker_images.py
- Check Docker images in `docker-compose.yml`
- Parse services with direct `image:` specification
- Detection and analysis of Dockerfiles referenced via `build:`
- Support for Docker Hub, GitHub Container Registry (ghcr.io), OCI registries
- Preservation of version suffixes (`-alpine`, `-slim`, etc.)
- `--create` option: generates `.new` files in the current directory
- `--update` option: backup of all modified files (compose + Dockerfiles)
- `--registry` option: use a custom registry
