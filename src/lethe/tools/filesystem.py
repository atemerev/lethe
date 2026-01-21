"""Filesystem tools for the agent."""


def _is_tool(func):
    """Decorator to mark a function as a Letta tool."""
    func._is_tool = True
    return func


@_is_tool
def read_file(file_path: str, offset: int = 0, limit: int = 2000) -> str:
    """Read a file from the filesystem.

    Args:
        file_path: Absolute path to the file to read
        offset: Line number to start reading from (0-indexed)
        limit: Maximum number of lines to read

    Returns:
        File contents with line numbers, or error message
    """
    from pathlib import Path
    
    try:
        path = Path(file_path).expanduser().resolve()
        if not path.exists():
            return f"Error: File not found: {file_path}"
        if not path.is_file():
            return f"Error: Not a file: {file_path}"

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        total_lines = len(lines)
        selected = lines[offset : offset + limit]

        result = []
        for i, line in enumerate(selected, start=offset + 1):
            if len(line) > 500:
                line = line[:10000] + "... [truncated]\n"
            result.append(f"{i:6d}\t{line.rstrip()}")

        header = f"File: {path} (lines {offset + 1}-{offset + len(selected)} of {total_lines})"
        return header + "\n" + "\n".join(result)

    except Exception as e:
        return f"Error reading file: {e}"


@_is_tool
def write_file(file_path: str, content: str) -> str:
    """Write content to a file, creating it if it doesn't exist.

    Args:
        file_path: Absolute path to the file to write
        content: Content to write to the file

    Returns:
        Success message or error
    """
    from pathlib import Path
    
    try:
        path = Path(file_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

        return f"Successfully wrote {len(content)} characters to {path}"

    except Exception as e:
        return f"Error writing file: {e}"


@_is_tool
def edit_file(file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """Edit a file by replacing text.

    Args:
        file_path: Absolute path to the file to edit
        old_string: Text to find and replace
        new_string: Text to replace with
        replace_all: If True, replace all occurrences; otherwise replace only the first

    Returns:
        Success message or error
    """
    from pathlib import Path
    
    try:
        path = Path(file_path).expanduser().resolve()
        if not path.exists():
            return f"Error: File not found: {file_path}"

        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        if old_string not in content:
            return f"Error: String not found in file: {repr(old_string[:100])}"

        if replace_all:
            new_content = content.replace(old_string, new_string)
            count = content.count(old_string)
        else:
            if content.count(old_string) > 1:
                return f"Error: String appears multiple times ({content.count(old_string)}). Use replace_all=True or provide more context."
            new_content = content.replace(old_string, new_string, 1)
            count = 1

        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)

        return f"Successfully replaced {count} occurrence(s) in {path}"

    except Exception as e:
        return f"Error editing file: {e}"


@_is_tool
def list_directory(path: str = ".", show_hidden: bool = False) -> str:
    """List contents of a directory.

    Args:
        path: Directory path to list
        show_hidden: Include hidden files (starting with .)

    Returns:
        Directory listing or error
    """
    from pathlib import Path
    
    try:
        dir_path = Path(path).expanduser().resolve()
        if not dir_path.exists():
            return f"Error: Directory not found: {path}"
        if not dir_path.is_dir():
            return f"Error: Not a directory: {path}"

        entries = []
        for entry in sorted(dir_path.iterdir()):
            if not show_hidden and entry.name.startswith("."):
                continue

            if entry.is_dir():
                entries.append(f"[DIR]  {entry.name}/")
            elif entry.is_symlink():
                entries.append(f"[LINK] {entry.name} -> {entry.resolve()}")
            else:
                size = entry.stat().st_size
                if size < 1024:
                    size_str = f"{size}B"
                elif size < 1024 * 1024:
                    size_str = f"{size // 1024}KB"
                else:
                    size_str = f"{size // (1024 * 1024)}MB"
                entries.append(f"[FILE] {entry.name} ({size_str})")

        return f"Contents of {dir_path}:\n" + "\n".join(entries) if entries else f"{dir_path} is empty"

    except Exception as e:
        return f"Error listing directory: {e}"


@_is_tool
def glob_search(pattern: str, path: str = ".") -> str:
    """Search for files matching a glob pattern.

    Args:
        pattern: Glob pattern (e.g., "**/*.py", "*.txt")
        path: Base directory to search from

    Returns:
        List of matching files or error
    """
    from pathlib import Path
    
    try:
        base_path = Path(path).expanduser().resolve()
        matches = list(base_path.glob(pattern))

        matches.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)

        if len(matches) > 100:
            matches = matches[:100]
            truncated = True
        else:
            truncated = False

        result = [str(m.relative_to(base_path) if m.is_relative_to(base_path) else m) for m in matches]

        header = f"Found {len(result)} files matching '{pattern}' in {base_path}"
        if truncated:
            header += " (showing first 100)"

        return header + "\n" + "\n".join(result) if result else f"No files matching '{pattern}' in {base_path}"

    except Exception as e:
        return f"Error in glob search: {e}"


@_is_tool
def grep_search(pattern: str, path: str = ".", file_pattern: str = "*") -> str:
    """Search for a regex pattern in files.

    Args:
        pattern: Regex pattern to search for
        path: Directory to search in
        file_pattern: Glob pattern to filter files (e.g., "*.py")

    Returns:
        Matching lines with file and line numbers
    """
    import re
    from pathlib import Path
    
    try:
        base_path = Path(path).expanduser().resolve()
        regex = re.compile(pattern)

        results = []
        files_searched = 0
        matches_found = 0

        for file_path in base_path.rglob(file_pattern):
            if not file_path.is_file():
                continue
            if file_path.name.startswith("."):
                continue

            files_searched += 1

            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    for line_num, line in enumerate(f, 1):
                        if regex.search(line):
                            rel_path = file_path.relative_to(base_path) if file_path.is_relative_to(base_path) else file_path
                            results.append(f"{rel_path}:{line_num}: {line.rstrip()}")
                            matches_found += 1

                            if matches_found >= 200:
                                break
            except Exception:
                continue

            if matches_found >= 200:
                break

        header = f"Found {matches_found} matches in {files_searched} files"
        if matches_found >= 200:
            header += " (limit reached)"

        return header + "\n" + "\n".join(results) if results else f"No matches for '{pattern}' in {base_path}"

    except Exception as e:
        return f"Error in grep search: {e}"
