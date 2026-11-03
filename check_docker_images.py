#!/usr/bin/env python3
"""
Check docker-compose.yml for outdated Docker images.

Usage:
    check_docker_images.py <docker-compose.yml>            # Display outdated images
    check_docker_images.py <docker-compose.yml> --create   # Create docker-compose.yml.new
    check_docker_images.py <docker-compose.yml> --update   # Backup and update in place
    check_docker_images.py <docker-compose.yml> --registry <url>  # Use custom registry

2026/11/02  V1.0.0  Initial version
"""

import argparse
import logging
import re
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

try:
    import requests
except ImportError:
    print("Error: 'requests' module is required. Install with: pip install requests", file=sys.stderr)
    sys.exit(1)

try:
    import yaml
except ImportError:
    print("Error: 'PyYAML' module is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

logger = logging.getLogger(__name__)


class ImageInfo(NamedTuple):
    """Docker image information."""
    full_name: str          # Full image string (e.g., "nginx:1.24")
    registry: str           # Registry (e.g., "docker.io", "ghcr.io")
    repository: str         # Repository (e.g., "library/nginx", "user/repo")
    tag: str                # Current tag (e.g., "1.24", "latest")
    source_file: Path       # File where image is defined
    service_name: str       # Service name or "Dockerfile"


def parse_image_name(image: str) -> tuple[str, str, str]:
    """
    Parse Docker image name into (registry, repository, tag).
    
    Examples:
        nginx:1.24 -> (docker.io, library/nginx, 1.24)
        nginx -> (docker.io, library/nginx, latest)
        user/repo:tag -> (docker.io, user/repo, tag)
        ghcr.io/user/repo:tag -> (ghcr.io, user/repo, tag)
        registry.example.com:5000/repo:tag -> (registry.example.com:5000, repo, tag)
    """
    # Split tag
    if '@' in image:
        # Digest reference (e.g., nginx@sha256:...)
        image = image.split('@')[0]
    
    if ':' in image:
        # Check if it's a port or a tag
        parts = image.rsplit(':', 1)
        # If the last part looks like a port number followed by more path, it's not a tag
        if '/' in parts[1]:
            # It's a registry with port
            image_part = image
            tag = 'latest'
        else:
            image_part = parts[0]
            tag = parts[1]
    else:
        image_part = image
        tag = 'latest'
    
    # Split registry and repository
    parts = image_part.split('/')
    
    if len(parts) == 1:
        # Official image (e.g., nginx)
        registry = 'docker.io'
        repository = f'library/{parts[0]}'
    elif len(parts) == 2:
        # Check if first part is a registry (contains . or :)
        if '.' in parts[0] or ':' in parts[0]:
            registry = parts[0]
            repository = parts[1]
        else:
            # User image (e.g., user/repo)
            registry = 'docker.io'
            repository = '/'.join(parts)
    else:
        # Full path with registry (e.g., ghcr.io/user/repo)
        registry = parts[0]
        repository = '/'.join(parts[1:])
    
    return registry, repository, tag


def get_docker_hub_tags(repository: str) -> list[str]:
    """Fetch tags from Docker Hub (multiple pages to get more coverage)."""
    all_tags = []
    url = f"https://hub.docker.com/v2/repositories/{repository}/tags"
    params = {'page_size': 100}
    
    try:
        # Fetch up to 3 pages to get enough tags
        for _ in range(3):
            response = requests.get(url, params=params, timeout=15)
            if response.status_code != 200:
                break
            data = response.json()
            all_tags.extend(tag['name'] for tag in data.get('results', []))
            
            # Check for next page
            next_url = data.get('next')
            if not next_url:
                break
            url = next_url
            params = {}  # Next URL includes params
    except Exception:
        pass
    
    return all_tags


def get_ghcr_tags(repository: str) -> list[str]:
    """Fetch tags from GitHub Container Registry."""
    # GHCR uses OCI distribution API
    url = f"https://ghcr.io/v2/{repository}/tags/list"
    
    try:
        response = requests.get(url, timeout=15)
        if response.status_code == 200:
            data = response.json()
            return data.get('tags', [])
    except Exception:
        pass
    return []


def get_registry_tags(registry: str, repository: str, custom_registry: str | None = None) -> list[str]:
    """Fetch tags from a Docker registry."""
    if custom_registry:
        registry = custom_registry
    
    if registry in ('docker.io', 'index.docker.io', 'registry.hub.docker.com'):
        return get_docker_hub_tags(repository)
    elif registry == 'ghcr.io':
        return get_ghcr_tags(repository)
    else:
        # Try generic OCI distribution API
        url = f"https://{registry}/v2/{repository}/tags/list"
        try:
            response = requests.get(url, timeout=15)
            if response.status_code == 200:
                data = response.json()
                return data.get('tags', [])
        except Exception:
            pass
    return []


def parse_version_tag(tag: str) -> tuple[list[int], str] | None:
    """
    Parse a version tag into comparable components.
    Returns (version_parts, suffix) or None if not a version tag.
    
    Examples:
        "1.24.0" -> ([1, 24, 0], "")
        "1.24-alpine" -> ([1, 24], "-alpine")
        "v2.3.1" -> ([2, 3, 1], "")
        "latest" -> None
    """
    # Skip non-version tags
    if tag in ('latest', 'stable', 'edge', 'dev', 'master', 'main'):
        return None
    
    # Remove leading 'v' or 'V'
    tag_clean = tag.lstrip('vV')
    
    # Match version pattern
    match = re.match(r'^(\d+(?:\.\d+)*)(.*)$', tag_clean)
    if not match:
        return None
    
    version_str = match.group(1)
    suffix = match.group(2)
    
    try:
        parts = [int(p) for p in version_str.split('.')]
        return parts, suffix
    except ValueError:
        return None


def find_latest_version(current_tag: str, available_tags: list[str]) -> str | None:
    """
    Find the latest version tag that matches the current tag's pattern.
    Returns None if current is already latest or no matching update found.
    """
    current_parsed = parse_version_tag(current_tag)
    if not current_parsed:
        return None
    
    current_parts, current_suffix = current_parsed
    
    # Find tags with matching suffix and higher version
    candidates = []
    for tag in available_tags:
        parsed = parse_version_tag(tag)
        if not parsed:
            continue
        
        parts, suffix = parsed
        
        # Must have same suffix (e.g., -alpine, -slim)
        if suffix != current_suffix:
            continue
        
        # Must have same number of version parts or be compatible
        # e.g., if current is 1.24, accept 1.25 but also 1.25.0
        if len(parts) < len(current_parts):
            continue
        
        # Compare versions
        if parts > current_parts:
            candidates.append((parts, tag))
    
    if not candidates:
        return None
    
    # Return the highest version
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def extract_images_from_dockerfile(dockerfile_path: Path) -> list[str]:
    """Extract FROM images from a Dockerfile."""
    images = []
    try:
        with open(dockerfile_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.upper().startswith('FROM '):
                    # Parse FROM instruction
                    # FROM image:tag [AS alias]
                    parts = line.split()
                    if len(parts) >= 2:
                        image = parts[1]
                        # Handle ARG substitution (skip if uses ARG)
                        if not image.startswith('$'):
                            images.append(image)
    except Exception:
        pass
    return images


def extract_images_from_compose(compose_path: Path) -> list[ImageInfo]:
    """Extract all Docker images from a docker-compose.yml file."""
    images = []
    compose_dir = compose_path.parent
    
    with open(compose_path, 'r', encoding='utf-8') as f:
        compose_data = yaml.safe_load(f)
    
    if not compose_data:
        return images
    
    services = compose_data.get('services', {})
    
    for service_name, service_config in services.items():
        if not isinstance(service_config, dict):
            continue
        
        # Check for direct image reference
        if 'image' in service_config:
            image = service_config['image']
            registry, repository, tag = parse_image_name(image)
            images.append(ImageInfo(
                full_name=image,
                registry=registry,
                repository=repository,
                tag=tag,
                source_file=compose_path,
                service_name=service_name
            ))
        
        # Check for build context with Dockerfile
        if 'build' in service_config:
            build_config = service_config['build']
            
            if isinstance(build_config, str):
                # Simple build context
                dockerfile_path = compose_dir / build_config / 'Dockerfile'
            elif isinstance(build_config, dict):
                context = build_config.get('context', '.')
                dockerfile = build_config.get('dockerfile', 'Dockerfile')
                dockerfile_path = compose_dir / context / dockerfile
            else:
                continue
            
            if dockerfile_path.exists():
                for image in extract_images_from_dockerfile(dockerfile_path):
                    registry, repository, tag = parse_image_name(image)
                    images.append(ImageInfo(
                        full_name=image,
                        registry=registry,
                        repository=repository,
                        tag=tag,
                        source_file=dockerfile_path,
                        service_name=f"{service_name} (Dockerfile)"
                    ))
    
    return images


def check_images(images: list[ImageInfo], custom_registry: str | None = None) -> list[dict]:
    """Check all images for updates."""
    results = []
    
    # Deduplicate by full_name + source_file
    seen = set()
    unique_images = []
    for img in images:
        key = (img.full_name, str(img.source_file))
        if key not in seen:
            seen.add(key)
            unique_images.append(img)
    
    logger.info("Checking %d image(s) against registries...", len(unique_images))
    
    def check_single_image(img: ImageInfo) -> dict:
        tags = get_registry_tags(img.registry, img.repository, custom_registry)
        latest = find_latest_version(img.tag, tags)
        
        return {
            'image': img,
            'available_tags': tags[:20],  # Keep first 20 for reference
            'latest_version': latest,
            'needs_update': latest is not None
        }
    
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_img = {
            executor.submit(check_single_image, img): img 
            for img in unique_images
        }
        
        for future in as_completed(future_to_img):
            try:
                results.append(future.result())
            except Exception:
                img = future_to_img[future]
                results.append({
                    'image': img,
                    'available_tags': [],
                    'latest_version': None,
                    'needs_update': False
                })
    
    return results


def display_results(results: list[dict]) -> None:
    """Display images that have updates available."""
    outdated = [r for r in results if r['needs_update']]
    
    if not outdated:
        logger.info("All images are up to date!")
        return
    
    header = f"\n{'Service':<30} {'Current':<25} {'Latest':<25}"
    separator = "-" * 80
    logger.info(header)
    logger.info(separator)
    
    for r in sorted(outdated, key=lambda x: x['image'].service_name):
        img = r['image']
        current = f"{img.repository.split('/')[-1]}:{img.tag}"
        latest = f"{img.repository.split('/')[-1]}:{r['latest_version']}"
        logger.info("%s %s %s", img.service_name.ljust(30), current.ljust(25), latest.ljust(25))
    
    logger.info("\n%d image(s) can be updated.", len(outdated))


def update_file_content(file_path: Path, updates: list[tuple[str, str]]) -> str:
    """Update image references in a file."""
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    for old_image, new_image in updates:
        # Escape special regex characters in old_image
        old_escaped = re.escape(old_image)
        # Replace the image reference
        content = re.sub(rf'\b{old_escaped}\b', new_image, content)
    
    return content


def create_new_files(compose_path: Path, results: list[dict]) -> list[Path]:
    """Create new files with updated versions in current directory."""
    created_files = []
    
    # Group updates by source file
    updates_by_file: dict[Path, list[tuple[str, str]]] = {}
    
    for r in results:
        if r['needs_update']:
            img = r['image']
            old_image = img.full_name
            # Build new image name
            new_image = old_image.replace(f":{img.tag}", f":{r['latest_version']}")
            
            if img.source_file not in updates_by_file:
                updates_by_file[img.source_file] = []
            updates_by_file[img.source_file].append((old_image, new_image))
    
    for file_path, updates in updates_by_file.items():
        content = update_file_content(file_path, updates)
        
        # Create .new file in current directory
        new_filename = f"{file_path.name}.new"
        output_path = Path.cwd() / new_filename
        
        # Handle multiple Dockerfiles by adding service name
        if output_path.exists() and file_path.name == 'Dockerfile':
            # Find unique name
            counter = 1
            while output_path.exists():
                new_filename = f"Dockerfile.{counter}.new"
                output_path = Path.cwd() / new_filename
                counter += 1
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        created_files.append(output_path)
    
    return created_files


def update_files_in_place(compose_path: Path, results: list[dict]) -> list[tuple[Path, Path]]:
    """
    Backup original files and update them in place.
    Returns list of (backup_path, updated_path).
    """
    updated_files = []
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Group updates by source file
    updates_by_file: dict[Path, list[tuple[str, str]]] = {}
    
    for r in results:
        if r['needs_update']:
            img = r['image']
            old_image = img.full_name
            new_image = old_image.replace(f":{img.tag}", f":{r['latest_version']}")
            
            if img.source_file not in updates_by_file:
                updates_by_file[img.source_file] = []
            updates_by_file[img.source_file].append((old_image, new_image))
    
    for file_path, updates in updates_by_file.items():
        # Create backup
        backup_name = f"{file_path.stem}.{timestamp}.bak"
        backup_path = file_path.parent / backup_name
        shutil.copy2(file_path, backup_path)
        
        # Update file
        content = update_file_content(file_path, updates)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        updated_files.append((backup_path, file_path))
    
    return updated_files


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s"
    )
    
    parser = argparse.ArgumentParser(
        description="Check docker-compose.yml for outdated Docker images"
    )
    parser.add_argument(
        'compose_file',
        type=Path,
        help="Path to docker-compose.yml file"
    )
    parser.add_argument(
        '--create',
        action='store_true',
        help="Create .new files with updated versions in current directory"
    )
    parser.add_argument(
        '--update',
        action='store_true',
        help="Backup original files and update them in place"
    )
    parser.add_argument(
        '--registry',
        type=str,
        default=None,
        help="Custom registry URL to use instead of default"
    )
    
    args = parser.parse_args()
    
    if args.create and args.update:
        logger.error("--create and --update are mutually exclusive")
        sys.exit(1)
    
    if not args.compose_file.exists():
        logger.error("File not found: %s", args.compose_file)
        sys.exit(1)
    
    # Extract images
    images = extract_images_from_compose(args.compose_file)
    
    if not images:
        logger.warning("No images found in the compose file.")
        sys.exit(0)
    
    # Check for updates
    results = check_images(images, args.registry)
    outdated_count = sum(1 for r in results if r['needs_update'])
    
    if args.create:
        if outdated_count == 0:
            logger.info("All images are up to date, no files created.")
        else:
            created_files = create_new_files(args.compose_file, results)
            logger.info("Created %d file(s) with %d updated image(s):", len(created_files), outdated_count)
            for f in created_files:
                logger.info("  - %s", f)
    elif args.update:
        if outdated_count == 0:
            logger.info("All images are up to date, no changes needed.")
        else:
            updated_files = update_files_in_place(args.compose_file, results)
            logger.info("Updated %d file(s) with %d image(s):", len(updated_files), outdated_count)
            for backup, updated in updated_files:
                logger.info("  - Backup: %s", backup)
                logger.info("    Updated: %s", updated)
    else:
        display_results(results)


if __name__ == '__main__':
    main()
