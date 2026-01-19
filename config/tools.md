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

## Browser Automation

Browser tools for web automation. Uses Steel Browser + Playwright with persistent sessions.

### Key Concepts

- **Persistent sessions**: Cookies, localStorage, and login state persist across tool calls
- **Accessibility Tree**: Use `browser_get_context()` for token-efficient page understanding (~90% smaller than raw DOM)
- **Close when done**: Call `browser_close()` to release the session when finished

### Workflow Example

```
1. browser_navigate("https://example.com/login")
2. browser_get_context()  # See what's on the page
3. browser_fill(value="user@email.com", label="Email")
4. browser_fill(value="password123", label="Password") 
5. browser_click(text="Sign In")
6. browser_wait_for(text="Dashboard")  # Wait for login to complete
7. browser_get_context()  # Now logged in, see dashboard elements
8. ... do more actions ...
9. browser_close()  # Release session when done
```

### Available Functions

| Function | Purpose |
|----------|---------|
| `browser_navigate(url)` | Go to a URL |
| `browser_get_context()` | Get page elements (token-efficient) |
| `browser_click(selector or text)` | Click an element |
| `browser_fill(value, selector or label)` | Type into an input |
| `browser_extract_text(selector)` | Get text content |
| `browser_screenshot()` | Take a screenshot |
| `browser_wait_for(selector or text)` | Wait for element to appear |
| `browser_close()` | Release browser session |

## Telegram Tools

Tools for sending files and media directly to the current Telegram chat.

### telegram_send_file

Send images, documents, or other files to the user.

```
telegram_send_file(file_path_or_url, caption="", as_document=False)
```

**Arguments:**
- `file_path_or_url`: Local file path or URL
  - Local: `/tmp/chart.png`, `~/documents/report.pdf`
  - URL: `https://example.com/image.jpg`
- `caption`: Optional text caption
- `as_document`: If True, send as document (preserves original quality for images)

**Auto-detection by extension:**
- Images (jpg, png, gif, webp) → sent as photos
- Videos (mp4, mov, mkv) → sent as videos
- Audio (mp3, ogg, wav) → sent as audio
- Other → sent as documents

**Examples:**
```python
# Send a local image
telegram_send_file("/tmp/screenshot.png", caption="Here's the screenshot")

# Send from URL
telegram_send_file("https://example.com/chart.png")

# Force document mode (higher quality, shows filename)
telegram_send_file("/tmp/photo.jpg", as_document=True)
```

---

*Add more tools here as they're installed or discovered.*
