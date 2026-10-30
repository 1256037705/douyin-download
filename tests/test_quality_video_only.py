# -*- coding: utf-8 -*-
"""最高清晰度选择、仅下载视频、任务列表滚动位置保留。

用法：
    python tests/test_quality_video_only.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from support import prepare_offline_f2

prepare_offline_f2()

from douyin_tool import core, config as cfg_mod  # noqa: E402

__test__ = False

_PASS = 0
_FAIL = 0


def check(name, got, want):
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print(f"  [ok]   {name}  ->  {got}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}  ->  got={got!r}  want={want!r}")


def check_true(name, cond):
    check(name, bool(cond), True)


# ---------------------------------------------------------------------------
# 1) select_highest_play_addr
# ---------------------------------------------------------------------------
def test_select_highest_play_addr():
    print("\n[1] select_highest_play_addr —— 按分辨率×码率选最高")

    # 无 bit_rate → 保持原 video_play_addr
    data = {"video_play_addr": ["http://keep.me/a.mp4"]}
    core.select_highest_play_addr(data, {"video": {}})
    check("无 bit_rate 保持原地址", data["video_play_addr"], ["http://keep.me/a.mp4"])

    core.select_highest_play_addr(data, None)
    check("raw 为 None 保持原地址", data["video_play_addr"], ["http://keep.me/a.mp4"])

    # 分辨率优先：1080p 码率更低也应胜出
    raw = {
        "aweme_id": "1",
        "video": {
            "bit_rate": [
                {
                    "bit_rate": 9_000_000,
                    "play_addr": {
                        "width": 720, "height": 1280,
                        "url_list": ["http://cdn/low.mp4", "http://cdn2/low.mp4"],
                    },
                },
                {
                    "bit_rate": 3_000_000,
                    "play_addr": {
                        "width": 1080, "height": 1920,
                        "url_list": ["http://cdn/high.mp4"],
                    },
                },
            ]
        },
    }
    data = {"video_play_addr": ["http://cdn/low.mp4"]}  # 模拟 f2 取 bit_rate[0]
    core.select_highest_play_addr(data, raw)
    check("分辨率优先于码率", data["video_play_addr"], ["http://cdn/high.mp4"])

    # 分辨率相同 → 比码率
    raw2 = {
        "video": {
            "bit_rate": [
                {
                    "bit_rate": 1_000_000,
                    "play_addr": {"width": 1080, "height": 1920, "url_list": ["http://a"]},
                },
                {
                    "bit_rate": 4_000_000,
                    "play_addr": {"width": 1080, "height": 1920, "url_list": ["http://b"]},
                },
            ]
        }
    }
    data2 = {"video_play_addr": ["http://a"]}
    core.select_highest_play_addr(data2, raw2)
    check("同分辨率比码率", data2["video_play_addr"], ["http://b"])

    # bit_rate[0] 不是最高时必须被纠正（回归 f2 默认行为）
    raw3 = {
        "video": {
            "bit_rate": [
                {"bit_rate": 500_000, "play_addr": {
                    "width": 540, "height": 960, "url_list": ["http://first"]}},
                {"bit_rate": 2_000_000, "play_addr": {
                    "width": 1080, "height": 1920, "url_list": ["http://best"]}},
            ]
        }
    }
    data3 = {"video_play_addr": ["http://first"]}
    core.select_highest_play_addr(data3, raw3)
    check("纠正 bit_rate[0] 非最高", data3["video_play_addr"], ["http://best"])


# ---------------------------------------------------------------------------
# 2) is_video_aweme / 仅视频
# ---------------------------------------------------------------------------
def test_is_video_aweme():
    print("\n[2] is_video_aweme —— 视频类型集合与 f2 一致")
    for t in (0, 55, 61, 109, 201):
        check_true(f"类型 {t} 是视频", core.is_video_aweme({"aweme_type": t}))
    check("类型 68 图文不是视频", core.is_video_aweme({"aweme_type": 68}), False)
    check("类型 2 非视频", core.is_video_aweme({"aweme_type": 2}), False)
    check("缺省类型视为视频（兼容）", core.is_video_aweme({"aweme_id": "1"}), True)


def test_build_kwargs_video_only():
    print("\n[3] build_kwargs —— music/cover/desc 保持 False")
    cfg = dict(cfg_mod.DEFAULT_CONFIG)
    cfg["cookie"] = "t=1"
    cfg["download_dir"] = tempfile.mkdtemp(prefix="dyd_kw_")
    kw = core.build_kwargs(cfg, mode="one")
    check("music=False", kw.get("music"), False)
    check("cover=False", kw.get("cover"), False)
    check("desc=False", kw.get("desc"), False)


# ---------------------------------------------------------------------------
# 3) download_one 图文友好报错 + 清晰度写回
# ---------------------------------------------------------------------------
def test_download_one_skips_image_and_picks_quality():
    print("\n[4] download_one —— 图文报错 + 清晰度写回")
    import f2.apps.douyin.handler as f2_handler

    tmp = tempfile.mkdtemp(prefix="dyd_one_")
    cfg = dict(cfg_mod.DEFAULT_CONFIG)
    cfg.update(download_dir=tmp, cookie="t=1")

    # 图文 → 友好报错
    detail_img = Mock()
    detail_img._to_raw.return_value = {"aweme_detail": {"aweme_type": 68, "video": {}}}
    detail_img._to_dict.return_value = {
        "aweme_id": "8880000000000000001",
        "nickname": "测",
        "desc": "图文",
        "aweme_type": 68,
    }
    handler_img = Mock()
    handler_img.downloader = None
    handler_img.fetch_one_video = AsyncMock(return_value=detail_img)

    real_h = f2_handler.DouyinHandler
    f2_handler.DouyinHandler = lambda kwargs: handler_img
    try:
        try:
            asyncio.run(core.download_one(
                "8880000000000000001", cfg, skip_downloaded=False))
            check("图文应抛错", False, True)
        except core.DouyinError as e:
            check_true("图文友好报错含提示", "仅支持下载视频" in str(e) or "图文" in str(e))
    finally:
        f2_handler.DouyinHandler = real_h

    # 视频：校验 select 被应用到 data 再交给 _save_video
    raw_video = {
        "aweme_detail": {
            "aweme_id": "8880000000000000002",
            "aweme_type": 0,
            "video": {
                "bit_rate": [
                    {"bit_rate": 100, "play_addr": {
                        "width": 540, "height": 960,
                        "url_list": ["http://low"]}},
                    {"bit_rate": 200, "play_addr": {
                        "width": 1080, "height": 1920,
                        "url_list": ["http://high"]}},
                ]
            },
        }
    }
    detail_v = Mock()
    detail_v._to_raw.return_value = raw_video
    detail_v._to_dict.return_value = {
        "aweme_id": "8880000000000000002",
        "nickname": "测",
        "desc": "视频",
        "aweme_type": 0,
        "video_play_addr": ["http://low"],
    }
    handler_v = Mock()
    handler_v.downloader = None
    handler_v.fetch_one_video = AsyncMock(return_value=detail_v)

    seen = {}

    async def fake_save(kwargs, data, base_dir, nickname="", desc=""):
        seen["play"] = data.get("video_play_addr")
        p = Path(base_dir) / "测" / "x_video.mp4"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        return str(p)

    f2_handler.DouyinHandler = lambda kwargs: handler_v
    real_s = core._save_video
    core._save_video = fake_save
    try:
        res = asyncio.run(core.download_one(
            "8880000000000000002", cfg, skip_downloaded=False))
        check("视频下载成功", res.get("skipped"), False)
        check("download_one 写回最高清晰度", seen.get("play"), ["http://high"])
    finally:
        f2_handler.DouyinHandler = real_h
        core._save_video = real_s


# ---------------------------------------------------------------------------
# 4) download_user_posts 图文记 skipped + 清晰度
# ---------------------------------------------------------------------------
def test_download_user_posts_skips_images():
    print("\n[5] download_user_posts —— 图文记 skipped，视频选最高清晰度")
    import f2.apps.douyin.handler as f2_handler

    rows = [
        ("7701000000000000001", "2026-09-29 12-00-00", "视频A", 0),
        ("7701000000000000002", "2026-09-28 12-00-00", "图文B", 68),
        ("7701000000000000003", "2026-09-27 12-00-00", "视频C", 0),
    ]

    class FakePage:
        def _to_list(self):
            return [
                {"aweme_id": a, "create_time": c, "desc": d,
                 "desc_full": d, "nickname": "测", "aweme_type": t,
                 "video_play_addr": ["http://first"]}
                for a, c, d, t in rows
            ]

        def _to_raw(self):
            return {
                "aweme_list": [
                    {
                        "aweme_id": a,
                        "aweme_type": t,
                        "video": {
                            "bit_rate": [
                                {"bit_rate": 100, "play_addr": {
                                    "width": 540, "height": 960,
                                    "url_list": ["http://first"]}},
                                {"bit_rate": 500, "play_addr": {
                                    "width": 1080, "height": 1920,
                                    "url_list": [f"http://best/{a}"]}},
                            ]
                        },
                    }
                    for a, c, d, t in rows
                ]
            }

    class FakeHandler:
        def __init__(self, kwargs):
            self.downloader = None

        async def fetch_user_post_videos(self, *a, **k):
            yield FakePage()

    saved_addrs = {}

    async def fake_save(kwargs, data, base_dir, nickname="", desc=""):
        aid = data["aweme_id"]
        saved_addrs[aid] = data.get("video_play_addr")
        p = Path(base_dir) / f"{aid}_video.mp4"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        return str(p)

    events = []

    def on_progress(cur, total, msg, item=None):
        if item:
            events.append(dict(item))

    tmp = tempfile.mkdtemp(prefix="dyd_batch_")
    cfg = dict(cfg_mod.DEFAULT_CONFIG)
    cfg.update(download_dir=tmp, cookie="t=1", download_workers=1,
               concurrency_mode="thread", default_max_counts=10)

    real_h, real_s = f2_handler.DouyinHandler, core._save_video
    f2_handler.DouyinHandler = FakeHandler
    core._save_video = fake_save
    try:
        res = asyncio.run(core.download_user_posts(
            "https://www.douyin.com/user/MS4wLjABAAAA_quality_test_xx",
            cfg, on_progress=on_progress, max_counts=10, skip_downloaded=False,
        ))
    finally:
        f2_handler.DouyinHandler = real_h
        core._save_video = real_s

    check("下载 2 条视频", res["download_count"], 2)
    check("图文进 skipped", "7701000000000000002" in res["skipped"], True)
    check("失败仍为 0（图文不当失败）", len(res["failed"]), 0)
    check("视频A 用最高清晰度",
          saved_addrs.get("7701000000000000001"),
          ["http://best/7701000000000000001"])
    check("视频C 用最高清晰度",
          saved_addrs.get("7701000000000000003"),
          ["http://best/7701000000000000003"])
    skip_events = [e for e in events
                   if e.get("aweme_id") == "7701000000000000002"
                   and e.get("status") == "skipped"]
    check_true("图文有 skipped 进度条目", bool(skip_events))
    check_true("图文跳过文案中文",
               any("图文" in (e.get("error") or "") for e in skip_events))


# ---------------------------------------------------------------------------
# 5) 媒体扩展名不含 webp；UI 滚动修复存在
# ---------------------------------------------------------------------------
def test_media_exts_and_ui_scroll():
    print("\n[6] 落盘只认视频扩展名 + UI 滚动位置保留")
    check_true("mp4 在媒体扩展名", ".mp4" in core._MEDIA_EXTS)
    check_true("webm 在媒体扩展名", ".webm" in core._MEDIA_EXTS)
    check(".webp 不在媒体扩展名", ".webp" in core._MEDIA_EXTS, False)
    check(".jpeg 不在媒体扩展名", ".jpeg" in core._MEDIA_EXTS, False)

    html = (Path(__file__).resolve().parents[1]
            / "src/douyin_tool/web/templates/index.html").read_text(encoding="utf-8")
    check_true("renderItems 带 data-tid",
               'class="item-list" data-tid="${t.id}"' in html
               or "class=\"item-list\" data-tid=\"${t.id}\"" in html)
    check_true("refreshTasks 保存 scrollTop", "savedScroll" in html)
    check_true("refreshTasks 恢复 item-list 滚动",
               "el.scrollTop = savedScroll" in html)
    check_true("仍保留 1.5s 轮询", "setInterval(refreshTasks, 1500)" in html)


def test_parse_note_label():
    print("\n[7] parse_link_info —— note 链接可标注")
    # 纯离线：extract 路径
    aid = "7682373106140319026"
    note_url = f"https://www.douyin.com/note/{aid}"
    # parse_link_info 是 async，本地能抠出 id 就不会联网
    info = asyncio.run(core.parse_link_info(note_url))
    check("note kind 仍为 video（下载路由）", info["kind"], "video")
    check_true("note 标注", info.get("note") is True)
    check_true("label 含图文笔记", "图文笔记" in info.get("label", ""))


def main():
    print("=" * 66)
    print("最高清晰度 / 仅视频 / 滚动位置")
    print("=" * 66)
    test_select_highest_play_addr()
    test_is_video_aweme()
    test_build_kwargs_video_only()
    test_download_one_skips_image_and_picks_quality()
    test_download_user_posts_skips_images()
    test_media_exts_and_ui_scroll()
    test_parse_note_label()

    print("\n" + "=" * 66)
    total = _PASS + _FAIL
    print(f"结果：{_PASS}/{total} 通过" + (f"，{_FAIL} 失败" if _FAIL else "，全绿 ✅"))
    print("=" * 66)
    if _FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
