"""Upload an Instagram Reel via the Instagram Graph API.

Usage:
    python reels_upload.py --video-url https://example.com/clip.mp4 \
        --caption "My new reel #fyp"

Requires a Business/Creator IG account linked to a Facebook Page, and an
access token with `instagram_content_publish`, `instagram_basic`, and
`pages_read_engagement` permissions.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass

import requests
from dotenv import load_dotenv


GRAPH_BASE = "https://graph.facebook.com"
POLL_INTERVAL_SEC = 5
POLL_TIMEOUT_SEC = 10 * 60  # Reels can take a while to process server-side.


class IGError(RuntimeError):
    pass


@dataclass(frozen=True)
class IGConfig:
    user_id: str
    access_token: str
    api_version: str

    @classmethod
    def from_env(cls) -> "IGConfig":
        load_dotenv()
        user_id = os.getenv("IG_USER_ID", "").strip()
        token = os.getenv("IG_ACCESS_TOKEN", "").strip()
        version = os.getenv("GRAPH_API_VERSION", "v21.0").strip()
        missing = [k for k, v in {"IG_USER_ID": user_id, "IG_ACCESS_TOKEN": token}.items() if not v]
        if missing:
            raise IGError(f"Missing env vars: {', '.join(missing)}")
        return cls(user_id=user_id, access_token=token, api_version=version)

    def url(self, path: str) -> str:
        return f"{GRAPH_BASE}/{self.api_version}/{path.lstrip('/')}"


def _raise_for_graph_error(resp: requests.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise IGError(f"Non-JSON response: {resp.text[:200]}")
    if "error" in data:
        err = data["error"]
        raise IGError(f"Graph API error {err.get('code')}: {err.get('message')}")
    resp.raise_for_status()
    return data


def create_reel_container(
    cfg: IGConfig,
    video_url: str,
    caption: str | None,
    share_to_feed: bool,
    cover_url: str | None,
) -> str:
    params = {
        "media_type": "REELS",
        "video_url": video_url,
        "share_to_feed": "true" if share_to_feed else "false",
        "access_token": cfg.access_token,
    }
    if caption:
        params["caption"] = caption
    if cover_url:
        params["cover_url"] = cover_url

    resp = requests.post(cfg.url(f"{cfg.user_id}/media"), data=params, timeout=60)
    data = _raise_for_graph_error(resp)
    container_id = data.get("id")
    if not container_id:
        raise IGError(f"No container id in response: {data}")
    return container_id


def wait_for_container_ready(cfg: IGConfig, container_id: str) -> None:
    deadline = time.monotonic() + POLL_TIMEOUT_SEC
    while True:
        resp = requests.get(
            cfg.url(container_id),
            params={"fields": "status_code,status", "access_token": cfg.access_token},
            timeout=30,
        )
        data = _raise_for_graph_error(resp)
        status = data.get("status_code")
        if status == "FINISHED":
            return
        if status in {"ERROR", "EXPIRED"}:
            raise IGError(f"Container {container_id} failed: {data.get('status') or status}")
        if time.monotonic() >= deadline:
            raise IGError(f"Timed out waiting for container {container_id} (last status: {status})")
        print(f"  ...processing (status={status}), retrying in {POLL_INTERVAL_SEC}s")
        time.sleep(POLL_INTERVAL_SEC)


def publish_container(cfg: IGConfig, container_id: str) -> str:
    resp = requests.post(
        cfg.url(f"{cfg.user_id}/media_publish"),
        data={"creation_id": container_id, "access_token": cfg.access_token},
        timeout=60,
    )
    data = _raise_for_graph_error(resp)
    media_id = data.get("id")
    if not media_id:
        raise IGError(f"No media id in publish response: {data}")
    return media_id


def upload_reel(
    cfg: IGConfig,
    video_url: str,
    caption: str | None,
    share_to_feed: bool = True,
    cover_url: str | None = None,
) -> str:
    print(f"Creating reel container for {video_url}")
    container_id = create_reel_container(cfg, video_url, caption, share_to_feed, cover_url)
    print(f"Container created: {container_id}")
    print("Waiting for Instagram to process the video...")
    wait_for_container_ready(cfg, container_id)
    print("Processing finished. Publishing...")
    media_id = publish_container(cfg, container_id)
    print(f"Published. Media ID: {media_id}")
    return media_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Upload an Instagram Reel via the Graph API.")
    parser.add_argument("--video-url", required=True, help="Publicly accessible MP4 URL.")
    parser.add_argument("--caption", default=None, help="Caption text (optional).")
    parser.add_argument("--cover-url", default=None, help="Cover image URL (optional).")
    parser.add_argument(
        "--no-share-to-feed",
        action="store_true",
        help="Do not also share the reel to the main feed.",
    )
    args = parser.parse_args(argv)

    try:
        cfg = IGConfig.from_env()
        upload_reel(
            cfg,
            video_url=args.video_url,
            caption=args.caption,
            share_to_feed=not args.no_share_to_feed,
            cover_url=args.cover_url,
        )
    except IGError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
