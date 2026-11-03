# check-outdated

Scripts to check and update outdated dependencies in your projects.

## Available Scripts

### check_requirements.py

Checks Python packages in `requirements.txt` or `pyproject.toml` files and detects outdated versions by querying PyPI.

**Dependencies:** `requests` (and `tomli` for Python < 3.11)

**Supported formats:**
- `requirements.txt` - Standard pip requirements format
- `pyproject.toml` - PEP 621 dependencies and Poetry dependencies

**Usage:**

```bash
# Display outdated packages (requirements.txt)
./check_requirements.py requirements.txt

# Display outdated packages (pyproject.toml)
./check_requirements.py pyproject.toml

# Create a <file>.new file with updated versions
./check_requirements.py requirements.txt --create
./check_requirements.py pyproject.toml --create

# Backup the original file and update in place
./check_requirements.py requirements.txt --update
./check_requirements.py pyproject.toml --update
```

**Example output:**

```
Checking 14 packages against PyPI...

Package                   Current         Latest         
-------------------------------------------------------
ijson                     3.3.0           3.6.0          
pycryptodome              3.18.0          4.2.0          
pymongo                   4.6.1           4.19.0         
requests                  2.32.3          2.34.2         

4 package(s) can be updated.
```

**Options:**

| Option     | Description                                                      |
|------------|------------------------------------------------------------------|
| (none)     | Displays outdated packages to stdout                             |
| `--create` | Creates `<file>.new` in the current directory                    |
| `--update` | Backs up the original (`*.YYYYMMDD_HHMMSS.bak`) and updates it   |

**pyproject.toml support:**

- PEP 621: `project.dependencies` and `project.optional-dependencies`
- Poetry: `tool.poetry.dependencies`, `tool.poetry.dev-dependencies`, and `tool.poetry.group.*.dependencies`

---

### check_docker_images.py

Checks Docker images in a `docker-compose.yml` file and its associated Dockerfiles. Detects outdated versions by querying Docker registries (Docker Hub by default).

**Dependencies:** `requests`, `pyyaml`

**Usage:**

```bash
# Display outdated images
./check_docker_images.py docker-compose.yml

# Create .new files with updated versions
./check_docker_images.py docker-compose.yml --create

# Backup and update in place
./check_docker_images.py docker-compose.yml --update

# Use a custom registry
./check_docker_images.py docker-compose.yml --registry registry.example.com
```

**Example output:**

```
Checking 4 image(s) against registries...

Service                        Current                   Latest                   
--------------------------------------------------------------------------------
app (Dockerfile)               python:3.9.0-slim         python:3.12.1-slim       
db                             postgres:14.0             postgres:16.1            
redis                          redis:6.2.0-alpine        redis:7.2.3-alpine       
web                            nginx:1.20.0              nginx:1.25.3             

4 image(s) can be updated.
```

**Options:**

| Option              | Description                                                        |
|---------------------|--------------------------------------------------------------------|
| (none)              | Displays outdated images to stdout                                 |
| `--create`          | Creates `.new` files in the current directory                      |
| `--update`          | Backs up the originals and updates in place                        |
| `--registry <url>`  | Uses a custom registry instead of Docker Hub                       |

**Features:**

- Parses services with direct `image:` specification
- Detects services with `build:` and analyzes referenced Dockerfiles
- Preserves version suffixes (`-alpine`, `-slim`, `-bullseye`, etc.)
- Supports Docker Hub, GitHub Container Registry (ghcr.io), and generic OCI registries
- Backs up both `docker-compose.yml` AND modified Dockerfiles

---

## Installation

```bash
# Clone or copy the scripts
cd /opt/check-outdated

# Install dependencies
python3 -m venv venv
source venv/bin/activate
pip install requests pyyaml

# For Python < 3.11, also install tomli for pyproject.toml support
pip install tomli

# Make the scripts executable
chmod +x *.py
```