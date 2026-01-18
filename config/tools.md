# Available CLI Tools

This file documents CLI tools available on the principal's machine. Use `run_command()` to invoke them.

## gog - Gmail CLI

Gmail operations from the command line.

```bash
# List recent emails
gog list

# List with limit
gog list -n 20

# Read a specific email by ID
gog read <message_id>

# Search emails
gog search "from:someone@example.com"
gog search "subject:important after:2024/01/01"

# Send an email
gog send -to "recipient@example.com" -subject "Subject" -body "Message body"

# Send with attachment
gog send -to "recipient@example.com" -subject "Subject" -body "See attached" -attach file.pdf
```

## git - Version Control

Standard git commands for repository management.

```bash
git status
git log --oneline -10
git diff
git add .
git commit -m "message"
git push
```

## uv - Python Package Manager

Fast Python package and project management.

```bash
uv sync              # Install dependencies
uv run <command>     # Run command in venv
uv add <package>     # Add dependency
uv pip list          # List installed packages
```

## docker - Container Management

```bash
docker ps                    # List running containers
docker logs <container>      # View container logs
docker exec -it <container> bash  # Shell into container
```

## Other Useful Commands

```bash
# System info
uname -a
df -h
free -h

# Process management
ps aux | grep <pattern>
kill <pid>

# Network
curl -s <url>
ping -c 3 <host>

# File operations (prefer built-in tools, but these work too)
find . -name "*.py"
wc -l <file>
```

---

*Add more tools here as they're installed or discovered.*
