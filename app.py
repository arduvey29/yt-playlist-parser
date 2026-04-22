#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════╗
║               YouTube Playlist Parser  ·  v1.0.0                        ║
║  Fetch · Verify · Analyze · Export YouTube playlist metadata             ║
╠══════════════════════════════════════════════════════════════════════════╣
║  Author  :  AARAADHY RAGHAV DUVEY                                        ║
║  License :  MIT                                                           ║
╚══════════════════════════════════════════════════════════════════════════╝

Features
--------
- Dual-source fetching  : yt-dlp (primary) + scrapetube (cross-verification)
- Concurrent fetching   : both sources run in parallel via ThreadPoolExecutor
- Smart verification    : title match-rate analysis + duration cross-check
- Speed table           : watch-time estimates at 0.75x → 8x
- Export                : JSON + CSV with full metadata
- CLI flags             : --url, --export, --durations, --debug, --version
- Graceful fallback     : single-source mode when one method fails

Usage
-----
  Interactive:  python yt_playlist_parser.py
  Direct URL:   python yt_playlist_parser.py --url "https://youtube.com/playlist?list=PLxxx"
  With export:  python yt_playlist_parser.py --url "..." --export json --durations
  Debug mode:   python yt_playlist_parser.py --debug
"""

import sys
import json
import csv
import logging
import argparse
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Tuple
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

# ── Dependency guard ──────────────────────────────────────────────────────────
try:
    import yt_dlp
    import scrapetube
    import pyfiglet
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
    from rich import box
except ImportError as e:
    missing = str(e).replace("No module named ", "").strip("'")
    print(f"\n  [ERROR] Missing dependency: {missing}")
    print("\n  Install everything with:\n")
    print("      pip install yt-dlp scrapetube pyfiglet rich\n")
    sys.exit(1)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s  [%(levelname)-8s]  %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("yt_playlist_parser")

console = Console(highlight=False)

# ── Constants ─────────────────────────────────────────────────────────────────
VERSION = "1.0.0"
FETCH_TIMEOUT_SECONDS = 90
MAX_VIDEO_CAP = 10_000                        # Safety limit for huge playlists
DURATION_MISMATCH_THRESHOLD_SECONDS = 60     # Warn if sources disagree >1 min
TITLE_MISMATCH_WARN_THRESHOLD_PCT = 10.0     # Warn if >10% of titles differ
PLAYBACK_SPEEDS = [0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0, 8.0]
VALID_PLAYLIST_PREFIXES = ('PL', 'RD', 'UU', 'FL', 'OL', 'LL', 'WL', 'TL')


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  DATA MODELS                                                             ║
# ╚══════════════════════════════════════════════════════════════════════════╝

@dataclass
class VideoEntry:
    """Represents a single video inside a playlist."""
    index: int
    title: str
    duration_seconds: int = 0
    video_id: str = ""
    source: str = ""
    available: bool = True


@dataclass
class PlaylistResult:
    """Aggregated result for an entire playlist fetch operation."""
    playlist_id: str
    url: str
    videos: List[VideoEntry] = field(default_factory=list)
    total_duration_seconds: int = 0
    fetch_method: str = ""
    verified: bool = False
    fetched_at: str = field(default_factory=lambda: datetime.now().isoformat())

    @property
    def video_count(self) -> int:
        return len(self.videos)

    @property
    def available_count(self) -> int:
        return sum(1 for v in self.videos if v.available)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  URL UTILITIES                                                           ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def extract_playlist_id(url: str) -> Optional[str]:
    """
    Extracts the playlist ID from any standard YouTube URL format.
    Handles www, mobile (m.), shortened (youtu.be), and schemeless URLs.

    Returns the playlist ID string, or None if extraction fails.
    """
    url = url.strip()
    # Prepend scheme if missing so urlparse works correctly
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        parsed = urlparse(url)
        valid_hosts = {
            "www.youtube.com", "youtube.com",
            "m.youtube.com",   "youtu.be",
        }
        if parsed.netloc not in valid_hosts:
            return None

        query = parse_qs(parsed.query)
        return query.get("list", [None])[0]
    except Exception as exc:
        logger.debug("URL parse error for '%s': %s", url, exc)
        return None


def validate_playlist_id(playlist_id: str) -> Tuple[bool, str]:
    """
    Validates basic structural expectations of a YouTube playlist ID.

    Returns:
        (True, "")           – valid, no warnings
        (True, warning_msg)  – probably valid but unusual
        (False, error_msg)   – definitely invalid, abort
    """
    if not playlist_id:
        return False, "Playlist ID is empty."
    if len(playlist_id) < 10:
        return False, f"Playlist ID '{playlist_id}' is unusually short (< 10 chars)."
    if not any(playlist_id.startswith(p) for p in VALID_PLAYLIST_PREFIXES):
        return True, (
            f"Unusual playlist ID prefix in '{playlist_id}'. "
            "YouTube may have introduced a new format — proceeding anyway."
        )
    return True, ""


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  FETCHERS                                                                ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def fetch_with_ytdlp(url: str) -> Tuple[Optional[List[VideoEntry]], int]:
    """
    Primary fetcher using yt-dlp.

    Uses extract_flat=True so all metadata is retrieved in a single bulk
    request — no per-video HTTP calls, no downloading.

    Returns:
        (list_of_VideoEntry, total_duration_seconds)
        or (None, 0) on failure.
    """
    ydl_opts = {
        "quiet":        True,
        "no_warnings":  True,
        "extract_flat": True,   # metadata only, no stream fetching
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info or "entries" not in info:
            logger.warning("yt-dlp: no entries returned.")
            return None, 0

        videos: List[VideoEntry] = []
        total_seconds = 0

        for i, entry in enumerate(info["entries"], start=1):
            if entry is None:
                videos.append(VideoEntry(
                    index=i, title="[Unavailable]",
                    source="yt-dlp", available=False,
                ))
                continue

            title    = entry.get("title")    or "[Untitled]"
            duration = int(entry.get("duration") or 0)
            vid_id   = entry.get("id")       or ""

            total_seconds += duration
            videos.append(VideoEntry(
                index=i, title=title,
                duration_seconds=duration,
                video_id=vid_id,
                source="yt-dlp",
                available=True,
            ))

        return (videos or None), total_seconds

    except yt_dlp.utils.DownloadError as exc:
        logger.warning("yt-dlp DownloadError: %s", exc)
        return None, 0
    except Exception as exc:
        logger.warning("yt-dlp unexpected error: %s", exc)
        return None, 0


def fetch_with_scrapetube(playlist_id: str) -> Tuple[Optional[List[VideoEntry]], int]:
    """
    Secondary fetcher using scrapetube for cross-verification.

    Iterates the lazy generator to keep memory usage low.
    Applies MAX_VIDEO_CAP as a safety limit for extremely large playlists.

    Returns:
        (list_of_VideoEntry, total_duration_seconds)
        or (None, 0) on failure.
    """
    try:
        gen = scrapetube.get_playlist(playlist_id)
        videos: List[VideoEntry] = []
        total_seconds = 0

        for i, raw in enumerate(gen, start=1):
            if i > MAX_VIDEO_CAP:
                logger.warning(
                    "scrapetube: reached %d-video cap — truncating.", MAX_VIDEO_CAP
                )
                break

            title     = "[Unavailable]"
            duration  = 0
            available = False

            try:
                if "title" in raw:
                    title     = raw["title"]["runs"][0]["text"]
                    available = True
                if "lengthSeconds" in raw:
                    duration = int(raw["lengthSeconds"])
                    total_seconds += duration
            except (KeyError, IndexError, ValueError, TypeError) as exc:
                logger.debug("scrapetube video %d parse error: %s", i, exc)

            vid_id = raw.get("videoId", "")
            videos.append(VideoEntry(
                index=i, title=title,
                duration_seconds=duration,
                video_id=vid_id,
                source="scrapetube",
                available=available,
            ))

        return (videos or None), total_seconds

    except Exception as exc:
        logger.warning("scrapetube error: %s", exc)
        return None, 0


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  CROSS-VERIFICATION                                                      ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def verify_and_merge(
    primary: List[VideoEntry],
    secondary: List[VideoEntry],
) -> Tuple[List[VideoEntry], bool, List[str]]:
    """
    Cross-verifies two independently fetched video lists.

    Checks:
      1. List-length agreement
      2. Per-title match rate (positional)
      3. Total duration agreement

    Always returns the primary (yt-dlp) list as the authoritative result.
    Warnings are collected and surfaced to the user — never silently swallowed.

    Returns:
        (authoritative_video_list, is_fully_verified, list_of_warning_strings)
    """
    warnings: List[str] = []

    # 1. Length check
    if len(primary) != len(secondary):
        warnings.append(
            f"Source length mismatch — yt-dlp: {len(primary)}, "
            f"scrapetube: {len(secondary)}. Using yt-dlp as authoritative source."
        )

    # 2. Title match rate
    comparable = min(len(primary), len(secondary))
    if comparable > 0:
        mismatches = sum(
            1 for i in range(comparable)
            if primary[i].title != secondary[i].title
        )
        pct = mismatches / comparable * 100
        if pct > TITLE_MISMATCH_WARN_THRESHOLD_PCT:
            warnings.append(
                f"Title mismatch rate: {pct:.1f}% ({mismatches}/{comparable} videos). "
                "Sources diverge — verify the playlist URL."
            )
    verified = (len(primary) == len(secondary) and comparable > 0 and
                all(primary[i].title == secondary[i].title for i in range(comparable)))

    # 3. Duration cross-check
    dur_primary   = sum(v.duration_seconds for v in primary)
    dur_secondary = sum(v.duration_seconds for v in secondary)
    if abs(dur_primary - dur_secondary) > DURATION_MISMATCH_THRESHOLD_SECONDS:
        warnings.append(
            f"Total duration mismatch — "
            f"yt-dlp: {format_duration(dur_primary)}, "
            f"scrapetube: {format_duration(dur_secondary)}. "
            "Using yt-dlp value."
        )

    return primary, verified, warnings


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  DURATION UTILITIES                                                      ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def format_duration(seconds: int) -> str:
    """Converts a raw second count to a concise human-readable string."""
    if seconds <= 0:
        return "0s"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}h {m}m {s}s"


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  DISPLAY                                                                 ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def print_banner() -> None:
    """Renders the ASCII art banner using pyfiglet + rich."""
    line1 = pyfiglet.figlet_format("YT PLAYLIST", font="slant")
    line2 = pyfiglet.figlet_format("PARSER", font="slant")
    console.print(f"[bold cyan]{line1}[/bold cyan]", end="")
    console.print(f"[bold cyan]{line2}[/bold cyan]", end="")
    console.print(Panel.fit(
        "[bold magenta]by AARAADHY RAGHAV DUVEY during end sem exam preparation[/bold magenta]\n"
        "[dim]MIT License[/dim]",
        border_style="magenta",
    ))
    console.print()


def display_results(result: PlaylistResult, show_durations: bool = False) -> None:
    """Renders the full playlist results using Rich tables and panels."""

    # ── Info panel ──────────────────────────────────────────────────────────
    verified_label = (
        "[bold green]✔  Fully verified  (both sources agree)[/bold green]"
        if result.verified
        else "[bold yellow]⚠  Single source  (cross-verification unavailable)[/bold yellow]"
    )
    unavailable = result.video_count - result.available_count
    unavail_line = (
        f"\n[bold]Unavailable:[/bold] [red]{unavailable} video(s)[/red]"
        if unavailable else ""
    )
    console.print(Panel(
        f"[bold]Playlist ID:[/bold]  {result.playlist_id}\n"
        f"[bold]Total videos:[/bold] {result.video_count}"
        f"  [dim]({result.available_count} available)[/dim]{unavail_line}\n"
        f"[bold]Total duration:[/bold] {format_duration(result.total_duration_seconds)}\n"
        f"[bold]Fetch method:[/bold] {result.fetch_method}\n"
        f"{verified_label}",
        title="[bold blue]● Playlist Summary[/bold blue]",
        border_style="blue",
        padding=(1, 2),
    ))

    # ── Video table ─────────────────────────────────────────────────────────
    table = Table(
        show_header=True,
        header_style="bold white on navy_blue",
        box=box.ROUNDED,
        expand=True,
        show_lines=False,
        padding=(0, 1),
    )
    table.add_column("#", style="bright_cyan", width=5, justify="right", no_wrap=True)
    table.add_column("Title", style="white", min_width=40, overflow="fold")
    if show_durations:
        table.add_column("Duration", style="bright_green", width=12, justify="right", no_wrap=True)

    for video in result.videos:
        row_style = "dim" if not video.available else ""
        title_display = f"[dim red]{video.title}[/dim red]" if not video.available else video.title
        if show_durations:
            dur_display = format_duration(video.duration_seconds) if video.available else "—"
            table.add_row(str(video.index), title_display, dur_display, style=row_style)
        else:
            table.add_row(str(video.index), title_display, style=row_style)

    console.print(table)
    console.print()

    # ── Speed table ─────────────────────────────────────────────────────────
    if result.total_duration_seconds > 0:
        display_speed_table(result.total_duration_seconds)


def display_speed_table(total_seconds: int) -> None:
    """Renders a playback-speed vs. watch-time table."""
    speed_table = Table(
        title="[bold]⏱  Watch Time at Different Playback Speeds[/bold]",
        show_header=True,
        header_style="bold white on dark_green",
        box=box.SIMPLE_HEAVY,
        padding=(0, 2),
    )
    speed_table.add_column("Speed",         justify="center", style="bold yellow", width=12)
    speed_table.add_column("Watch Time",    justify="center", style="bright_green", width=16)
    speed_table.add_column("Time Saved",    justify="center", style="bright_cyan",  width=16)
    speed_table.add_column("% Remaining",   justify="center", style="white",         width=14)

    for speed in PLAYBACK_SPEEDS:
        adjusted  = int(total_seconds / speed)
        saved     = total_seconds - adjusted
        remaining = (adjusted / total_seconds * 100) if total_seconds else 0.0
        label     = f"[bold]{speed}x  ◀ normal[/bold]" if speed == 1.0 else f"{speed}x"
        saved_str = "[dim]—[/dim]" if speed == 1.0 else format_duration(saved)
        speed_table.add_row(
            label,
            format_duration(adjusted),
            saved_str,
            f"{remaining:.0f}%",
        )

    console.print(speed_table)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  EXPORT                                                                  ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def _build_export_filename(playlist_id: str, fmt: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"playlist_{playlist_id}_{ts}.{fmt}"


def export_json(result: PlaylistResult, filepath: str) -> None:
    """Serialises the full PlaylistResult to a pretty-printed JSON file."""
    payload = {
        "meta": {
            "playlist_id":               result.playlist_id,
            "url":                        result.url,
            "fetched_at":                 result.fetched_at,
            "fetch_method":               result.fetch_method,
            "verified":                   result.verified,
            "generator":                  f"yt-playlist-parser v{VERSION}",
        },
        "stats": {
            "video_count":                result.video_count,
            "available_count":            result.available_count,
            "total_duration_seconds":     result.total_duration_seconds,
            "total_duration_formatted":   format_duration(result.total_duration_seconds),
        },
        "speed_estimates": {
            str(s) + "x": format_duration(int(result.total_duration_seconds / s))
            for s in PLAYBACK_SPEEDS
        },
        "videos": [asdict(v) for v in result.videos],
    }
    with open(filepath, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    console.print(f"[bold green]✔  JSON export:[/bold green] [underline]{filepath}[/underline]")


def export_csv(result: PlaylistResult, filepath: str) -> None:
    """Exports the video list to a UTF-8 CSV file."""
    fieldnames = [
        "index", "title", "video_id",
        "duration_seconds", "duration_formatted", "available", "source",
    ]
    with open(filepath, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for v in result.videos:
            writer.writerow({
                "index":               v.index,
                "title":               v.title,
                "video_id":            v.video_id,
                "duration_seconds":    v.duration_seconds,
                "duration_formatted":  format_duration(v.duration_seconds),
                "available":           v.available,
                "source":              v.source,
            })
    console.print(f"[bold green]✔  CSV export:[/bold green] [underline]{filepath}[/underline]")


def handle_export(result: PlaylistResult, fmt: str) -> None:
    """Routes to the correct exporter and reports errors gracefully."""
    fmt = fmt.lower().strip()
    if fmt not in ("json", "csv"):
        console.print(f"[red]Unknown export format '{fmt}'. Use 'json' or 'csv'.[/red]")
        return
    filepath = _build_export_filename(result.playlist_id, fmt)
    try:
        if fmt == "json":
            export_json(result, filepath)
        else:
            export_csv(result, filepath)
    except OSError as exc:
        console.print(f"[bold red]Export failed:[/bold red] {exc}")


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  CORE PIPELINE                                                           ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def process_playlist(
    url: str,
    show_durations: bool = False,
    export_fmt: Optional[str] = None,
) -> Optional[PlaylistResult]:
    """
    Main processing pipeline:

      1.  Validate URL  →  extract & validate playlist ID
      2.  Concurrent fetch  →  yt-dlp (primary) + scrapetube (secondary)
              both run in separate threads via ThreadPoolExecutor
      3.  Cross-verify  →  compare titles + durations, surface warnings
      4.  Graceful fallback  →  single-source if one method fails
      5.  Display  →  rich table + speed table
      6.  Export  →  JSON / CSV (optional)

    Returns the populated PlaylistResult on success, None on failure.
    """
    # ── Step 1: URL validation ────────────────────────────────────────────
    playlist_id = extract_playlist_id(url)
    if not playlist_id:
        console.print(Panel(
            "[bold red]Could not extract a playlist ID from the URL.[/bold red]\n\n"
            "Make sure the URL:\n"
            "  • Contains [bold]list=[/bold] as a query parameter\n"
            "  • Is a valid youtube.com or youtu.be URL\n\n"
            f"[dim]Received: {url}[/dim]",
            title="[red]Invalid URL[/red]",
            border_style="red",
        ))
        return None

    is_valid, id_warning = validate_playlist_id(playlist_id)
    if not is_valid:
        console.print(f"[bold red]✖  {id_warning}[/bold red]")
        return None
    if id_warning:
        console.print(f"[yellow]⚠  {id_warning}[/yellow]")

    # ── Step 2: Concurrent fetch ──────────────────────────────────────────
    videos_ytdlp:      Optional[List[VideoEntry]] = None
    duration_ytdlp:    int                        = 0
    videos_scrape:     Optional[List[VideoEntry]] = None
    duration_scrape:   int                        = 0

    with Progress(
        SpinnerColumn(style="cyan"),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=20, style="blue"),
        TextColumn("[dim]{task.elapsed:.1f}s[/dim]"),
        console=console,
        transient=True,
    ) as prog:
        t1 = prog.add_task("[cyan]  yt-dlp      fetching…", total=None)
        t2 = prog.add_task("[blue]  scrapetube  fetching…", total=None)

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="yt_fetch") as pool:
            future_ytdlp  = pool.submit(fetch_with_ytdlp,    url)
            future_scrape = pool.submit(fetch_with_scrapetube, playlist_id)

            try:
                videos_ytdlp, duration_ytdlp = future_ytdlp.result(timeout=FETCH_TIMEOUT_SECONDS)
                prog.update(t1, description="[green]  yt-dlp      done ✔")
            except FutureTimeoutError:
                prog.update(t1, description="[yellow]  yt-dlp      timed out ⚠")
                future_ytdlp.cancel()
                logger.warning("yt-dlp timed out after %ds.", FETCH_TIMEOUT_SECONDS)
            except Exception as exc:
                prog.update(t1, description="[red]  yt-dlp      failed ✖")
                logger.warning("yt-dlp future error: %s", exc)

            try:
                videos_scrape, duration_scrape = future_scrape.result(timeout=FETCH_TIMEOUT_SECONDS)
                prog.update(t2, description="[green]  scrapetube  done ✔")
            except FutureTimeoutError:
                prog.update(t2, description="[yellow]  scrapetube  timed out ⚠")
                future_scrape.cancel()
                logger.warning("scrapetube timed out after %ds.", FETCH_TIMEOUT_SECONDS)
            except Exception as exc:
                prog.update(t2, description="[red]  scrapetube  failed ✖")
                logger.warning("scrapetube future error: %s", exc)

    # ── Step 3 / 4: Verify + fallback ─────────────────────────────────────
    final_videos:   List[VideoEntry] = []
    final_duration: int              = 0
    method_used:    str              = ""
    verified:       bool             = False

    if videos_ytdlp is not None and videos_scrape is not None:
        final_videos, verified, warnings = verify_and_merge(videos_ytdlp, videos_scrape)
        for w in warnings:
            console.print(f"[yellow]  ⚠  {w}[/yellow]")
        final_duration = duration_ytdlp
        method_used    = "yt-dlp + scrapetube"

    elif videos_ytdlp is not None:
        final_videos   = videos_ytdlp
        final_duration = duration_ytdlp
        method_used    = "yt-dlp"
        console.print("[yellow]  ⚠  scrapetube failed — using yt-dlp only (no cross-verification).[/yellow]")

    elif videos_scrape is not None:
        final_videos   = videos_scrape
        final_duration = duration_scrape
        method_used    = "scrapetube"
        console.print("[yellow]  ⚠  yt-dlp failed — using scrapetube only (no cross-verification).[/yellow]")

    else:
        console.print(Panel(
            "[bold red]Both fetch methods failed.[/bold red]\n\n"
            "Possible causes:\n"
            "  • Playlist is [bold]private[/bold] or [bold]deleted[/bold]\n"
            "  • Network / proxy issue\n"
            "  • YouTube changed its API structure\n\n"
            "[dim]Try:  pip install -U yt-dlp[/dim]",
            title="[red]Fetch Failed[/red]",
            border_style="red",
        ))
        return None

    # ── Step 5: Build result + display ────────────────────────────────────
    result = PlaylistResult(
        playlist_id=playlist_id,
        url=url,
        videos=final_videos,
        total_duration_seconds=final_duration,
        fetch_method=method_used,
        verified=verified,
    )

    display_results(result, show_durations=show_durations)

    # ── Step 6: Export ────────────────────────────────────────────────────
    if export_fmt:
        handle_export(result, export_fmt)

    return result


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  CLI                                                                     ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yt-playlist-parser",
        description="Fetch, verify, and export YouTube playlist metadata.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python yt_playlist_parser.py
  python yt_playlist_parser.py --url "https://youtube.com/playlist?list=PLxxxx"
  python yt_playlist_parser.py --url "..." --durations
  python yt_playlist_parser.py --url "..." --export json
  python yt_playlist_parser.py --url "..." --export csv --durations
  python yt_playlist_parser.py --debug
        """,
    )
    parser.add_argument(
        "--url", "-u", metavar="URL",
        help="YouTube playlist URL (skips interactive prompt).",
    )
    parser.add_argument(
        "--export", "-e", metavar="FORMAT", choices=["json", "csv"],
        help="Export results to a file. Choices: json, csv.",
    )
    parser.add_argument(
        "--durations", "-d", action="store_true",
        help="Show per-video durations in the video table.",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable verbose debug-level logging.",
    )
    parser.add_argument(
        "--version", "-v", action="version",
        version=f"%(prog)s {VERSION}",
    )
    return parser


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  ENTRY POINT                                                             ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def main() -> None:
    parser = build_arg_parser()
    args   = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.setLevel(logging.DEBUG)
        console.print("[dim]Debug logging enabled.[/dim]\n")

    print_banner()

    if args.url:
        # Non-interactive single-shot mode
        process_playlist(args.url, show_durations=args.durations, export_fmt=args.export)
    else:
        # Interactive REPL loop
        console.print(
            "[dim]Paste a YouTube playlist URL below.  "
            "Type [bold]quit[/bold] or press [bold]Ctrl+C[/bold] to exit.[/dim]\n"
        )
        while True:
            try:
                user_input = console.input(
                    "[bold bright_white]Playlist URL ▶ [/bold bright_white]"
                ).strip()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[cyan]Goodbye![/cyan]")
                break

            if not user_input:
                continue
            if user_input.lower() in ("quit", "exit", "q"):
                console.print("[cyan]Goodbye![/cyan]")
                break

            process_playlist(user_input, show_durations=args.durations, export_fmt=args.export)
            console.print()


if __name__ == "__main__":
    main()