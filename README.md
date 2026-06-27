# BingeSentry

An automated media pre-caching utility for Plex media servers. It monitors currently playing media, identifies upcoming TV episodes, checks disk space availability, maps mount paths, and initiates caching processes (via rclone or a custom command) to ensure buffering-free binge-watching.

## Key Features

*   **Smart Episode Caching**: Identifies shows currently being streamed and caches the next sequential episode. Supports **multi-season transitions** (caching Season 2 Episode 1 while watching Season 1's finale).
*   **Smart Gap-Caching**: Analyzes rclone VFS metadata (`vfsMeta`) dynamically to read file range download states, caching *only* the missing byte ranges (gaps) rather than naively re-downloading the entire file sequentially.
*   **Dynamic Bandwidth Throttling**: Monitors active Plex streams and dynamically rate-limits download speeds when users are streaming. This guarantees a **zero-interruption** experience by preventing network and disk IO bottlenecks.
*   **Intelligent Queue Prioritization**: Multi-episode pre-caching tasks are ranked using a priority formula: `progress_pct - (offset * 100)`. This guarantees immediate next episodes are cached first across all concurrent users, followed by secondary buffer targets.
*   **Mount Health Guard**: Periodically runs non-blocking liveness stats on FUSE mounts using isolated subprocesses with a strict 2-second timeout. If the network mount locks up or disconnects, the daemon automatically pauses tasks and flashes a red alert on the TUI dashboard to prevent the app from freezing.
*   **Flexible Caching Commands**: Configurable cache trigger command (`python cache_executor.py --file {file_path}` by default, but customizable to any CLI tool).
*   **Persistent Daemon Service**: Runs as a persistent event-driven background service listening for play activity.
*   **Plex WebSocket Alerts (Zero-Poll Support)**: Automatically listens to Plex WebSocket notifications (`AlertListener`) to catch real-time playback and progress events directly from the Plex server. This enables zero-latency updates and eliminates periodic polling entirely to touch Plex as lightly as possible.
*   **Terminal User Interface (TUI)**: Beautiful real-time console dashboard (`TUI_MODE=True`) detailing active streaming sessions, next cache targets, background download processes, and logs.
*   **Path Mapping**: Translate Plex media paths (e.g. `/data/media`) to local client mount paths (e.g. `/mnt/unionfs`) seamlessly.
*   **Intelligent Disk Checks**: Inspects the free space of the exact directory mount point holding the target episode rather than just the root partition.
*   **Dual-Channel Logging**: Clean rotating files (with ANSI escape sequences automatically stripped) combined with colored, readable console output for terminal monitoring.
*   **Docker Ready**: Dockerfile and docker-compose configurations ready out-of-the-box.

---

## Configuration Options (`config.ini`)

Configure using [config.ini](file:///home/mediaserver/plexPreCacherclonenextEpisode/config.ini) or environment variables:

| Section | Parameter | Description | Default |
| :--- | :--- | :--- | :--- |
| **`[Plex]`** | `PLEX_URL` | URL of your Plex server | `http://127.0.0.1:32400` |
| | `PLEX_TOKEN` | Plex API authentication token | *(Required)* |
| **`[Logging]`**| `LOG_FILE` | Path to rotating log file | *(None / Console Only)* |
| | `LOG_LEVEL` | Level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) | `INFO` |
| | `LOG_TO_CONSOLE` | Enable console/stdout outputs | `True` |
| **`[Cache]`** | `CACHE_COMMAND` | Caching shell command template | `python cache_executor.py --file {file_path}` |
| | `THROTTLE_SPEED_ACTIVE_MB` | Caching download limit in MB/s when active streams are detected | `0.0` (unlimited) |
| | `MIN_FREE_SPACE_GB`| Required disk space (GB) to caching | `5.0` |
| | `EPISODES_TO_CACHE`| Number of upcoming episodes to cache | `1` |
| | `CACHE_START_THRESHOLD_PCT`| Percentage progress of episode before caching | `50` |
| | `PATH_MAP_FROM` | Path prefix mapping target from Plex | *(None)* |
| | `PATH_MAP_TO` | Mapped local destination path prefix | *(None)* |
| **`[Daemon]`** | `TUI_MODE` | Renders a real-time Terminal dashboard | `False` |


---

## Installation

### 1. Setup Dependencies
Create a virtual environment and install python dependencies:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Settings
Copy the example configuration file and fill in your Plex URL and API token:
```bash
cp config.example.ini config.ini
nano config.ini
```

---

## Deployment Options

### Option A: Docker Container (Recommended)
Start using Docker Compose:
```bash
docker compose up -d
```
You can override settings via environment variables in `docker-compose.yml` (e.g. `PLEX_TOKEN=...`).

To access the interactive Terminal User Interface (TUI) dashboard while running in the background, attach to the container:
```bash
docker attach bingesentry
```
To detach from the console without stopping BingeSentry, press:
* **`Ctrl + P`** followed by **`Ctrl + Q`**

### Option B: Run Directly on Host
Execute:
```bash
python3 main.py
```
This is the most efficient method, running a low-overhead event-driven loop that preserves connections.

---

## Project Structure

*   [main.py](file:///home/mediaserver/plexPreCacherclonenextEpisode/main.py): Service orchestrator and entry point.
*   [cache_executor.py](file:///home/mediaserver/plexPreCacherclonenextEpisode/cache_executor.py): Smart caching process runner that handles gap-based caching and dynamic bandwidth throttling.
*   [config.py](file:///home/mediaserver/plexPreCacherclonenextEpisode/config.py): Clean, strongly-typed configuration loader with env-var override capability.
*   [plex_utils.py](file:///home/mediaserver/plexPreCacherclonenextEpisode/plex_utils.py): Plex server API connection and season progression logic.
*   [disk.py](file:///home/mediaserver/plexPreCacherclonenextEpisode/disk.py): Disk usage checkers and path translation mapping utilities.
*   [rclone.py](file:///home/mediaserver/plexPreCacherclonenextEpisode/rclone.py): Caching command process execution and active duplicate detection.
*   [logger.py](file:///home/mediaserver/plexPreCacherclonenextEpisode/logger.py): Log handlers with dual output formatters (stripping colors from file output).
