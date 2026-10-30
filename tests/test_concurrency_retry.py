# -*- coding: utf-8 -*-
"""并发下载、条目进度与失败重试。"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from support import prepare_offline_f2

prepare_offline_f2()

from douyin_tool import config, core, validation, web_app as web

__test__ = False


class ValidationTests(unittest.TestCase):
    def test_download_workers_bounds(self):
        self.assertEqual(validation.download_workers(None), 2)
        self.assertEqual(validation.download_workers(4), 4)
        with self.assertRaises(ValueError):
            validation.download_workers(0)
        with self.assertRaises(ValueError):
            validation.download_workers(9)

    def test_concurrency_mode(self):
        self.assertEqual(validation.concurrency_mode("thread"), "thread")
        self.assertEqual(validation.concurrency_mode("async"), "thread")
        self.assertEqual(validation.concurrency_mode("process"), "process")
        with self.assertRaises(ValueError):
            validation.concurrency_mode("magic")

    def test_max_parallel_tasks_bounds(self):
        self.assertEqual(validation.max_parallel_tasks(None), 1)
        self.assertEqual(validation.max_parallel_tasks(3), 3)
        with self.assertRaises(ValueError):
            validation.max_parallel_tasks(0)
        with self.assertRaises(ValueError):
            validation.max_parallel_tasks(5)


class ProgressItemTests(unittest.TestCase):
    def test_aweme_url(self):
        self.assertEqual(core.aweme_url("123"), "https://www.douyin.com/video/123")

    def test_download_user_posts_emits_items_and_honors_workers(self):
        """假 handler + 假 save：条目进度齐全，并发数限制生效。"""
        import f2.apps.douyin.handler as f2_handler

        rows = [
            (f"770000000000000000{i}", f"2026-09-2{i} 12-00-00", f"文案{i}")
            for i in range(4)
        ]

        class FakePage:
            def _to_list(self):
                return [
                    {"aweme_id": a, "create_time": c, "desc": d,
                     "desc_full": d, "nickname": "测"}
                    for a, c, d in rows
                ]

        class FakeHandler:
            def __init__(self, kwargs):
                self.kwargs = kwargs
                self.downloader = None

            async def fetch_user_post_videos(self, *a, **k):
                yield FakePage()

        active = peak = 0
        lock = asyncio.Lock()
        events = []

        async def fake_save(kwargs, data, base_dir, nickname="", desc=""):
            nonlocal active, peak
            async with lock:
                active += 1
                peak = max(peak, active)
            await asyncio.sleep(0.05)
            async with lock:
                active -= 1
            p = Path(base_dir) / f"{data['aweme_id']}_video.mp4"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\x00\x00\x00\x18ftypmp42")
            return str(p)

        def on_progress(cur, total, msg, item=None):
            if item:
                events.append(dict(item))

        tmp = tempfile.mkdtemp(prefix="dyd_workers_")
        cfg = dict(config.DEFAULT_CONFIG)
        cfg.update(download_dir=tmp, cookie="t=1", download_workers=2,
                   concurrency_mode="thread", default_max_counts=4)

        real_h, real_s = f2_handler.DouyinHandler, core._save_video
        f2_handler.DouyinHandler = FakeHandler
        core._save_video = fake_save
        try:
            res = asyncio.run(core.download_user_posts(
                "https://www.douyin.com/user/MS4wLjABAAAA_test_user_xx",
                cfg, on_progress=on_progress, max_counts=4, skip_downloaded=False,
            ))
        finally:
            f2_handler.DouyinHandler = real_h
            core._save_video = real_s

        self.assertEqual(res["download_count"], 4)
        self.assertEqual(res["workers"], 2)
        self.assertLessEqual(peak, 2)
        self.assertGreaterEqual(peak, 2)  # 确实用上了并发
        statuses = {e["aweme_id"]: e["status"] for e in events if e.get("aweme_id")}
        self.assertTrue(all(s == "done" for s in statuses.values()))
        self.assertTrue(all(e.get("url", "").startswith("https://www.douyin.com/video/")
                            for e in events if e.get("aweme_id")))

    def test_failed_item_carries_url(self):
        import f2.apps.douyin.handler as f2_handler

        class FakePage:
            def _to_list(self):
                return [{"aweme_id": "7710000000000000001",
                         "create_time": "2026-09-29 12-00-00",
                         "desc": "坏", "nickname": "测"}]

        class FakeHandler:
            def __init__(self, kwargs):
                self.downloader = None

            async def fetch_user_post_videos(self, *a, **k):
                yield FakePage()

        async def boom(*a, **k):
            raise RuntimeError("模拟失败")

        events = []

        def on_progress(cur, total, msg, item=None):
            if item:
                events.append(dict(item))

        tmp = tempfile.mkdtemp(prefix="dyd_fail_")
        cfg = dict(config.DEFAULT_CONFIG)
        cfg.update(download_dir=tmp, cookie="t=1", download_workers=1)

        real_h, real_s = f2_handler.DouyinHandler, core._save_video
        f2_handler.DouyinHandler = FakeHandler
        core._save_video = boom
        try:
            res = asyncio.run(core.download_user_posts(
                "https://www.douyin.com/user/MS4wLjABAAAA_test_user_yy",
                cfg, on_progress=on_progress, max_counts=1, skip_downloaded=False,
            ))
        finally:
            f2_handler.DouyinHandler = real_h
            core._save_video = real_s

        self.assertEqual(len(res["failed"]), 1)
        self.assertIn("url", res["failed"][0])
        self.assertTrue(res["failed"][0]["url"].endswith("7710000000000000001"))
        failed_events = [e for e in events if e.get("status") == "failed"]
        self.assertTrue(failed_events)
        self.assertIn("模拟失败", failed_events[-1].get("error", ""))


class RetryApiTests(unittest.TestCase):
    def setUp(self):
        with web._TASKS_LOCK:
            web._TASKS.clear()

    def wait_finished(self, tid, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            task = web._get(tid)
            if task["status"] not in ("pending", "running"):
                return task
            time.sleep(0.02)
        raise AssertionError("任务未结束")

    def test_retry_failed_updates_items(self):
        tid = web._new_task("post", "https://www.douyin.com/user/MS4wLjABAAAA_x")
        web._update(
            tid, status="done", message="完成（部分作品失败）",
            items=[{
                "aweme_id": "7720000000000000001",
                "url": "https://www.douyin.com/video/7720000000000000001",
                "status": "failed", "error": "旧错误", "nickname": "a", "desc": "d",
                "path": "",
            }],
            result={"failed": [{"aweme_id": "7720000000000000001", "error": "旧错误"}],
                    "download_count": 0, "skipped": []},
        )

        async def fake_list(ids, cfg, on_progress=None, should_stop=None, meta=None):
            aid = ids[0]
            if on_progress:
                on_progress(0, 1, "重试中", {
                    "aweme_id": aid, "url": core.aweme_url(aid),
                    "status": "downloading", "nickname": "a", "desc": "d",
                })
                on_progress(1, 1, "完成", {
                    "aweme_id": aid, "url": core.aweme_url(aid),
                    "status": "done", "nickname": "a", "desc": "d",
                    "path": "/tmp/x.mp4",
                })
            return {"downloaded": [{"aweme_id": aid, "path": "/tmp/x.mp4"}],
                    "failed": [], "skipped": [], "download_count": 1,
                    "total_handled": 1}

        with patch.object(config, "load_config", return_value={"cookie": "x=1"}), \
             patch.object(core, "download_aweme_list", side_effect=fake_list):
            client = web.app.test_client()
            resp = client.post(f"/api/retry/{tid}", json={})
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.get_json()["ok"])
            task = self.wait_finished(tid)
            self.assertEqual(task["status"], "done")
            statuses = {i["aweme_id"]: i["status"] for i in task["items"]}
            self.assertEqual(statuses.get("7720000000000000001"), "done")

    def test_retry_specific_aweme_id(self):
        tid = web._new_task("post", "https://www.douyin.com/user/MS4wLjABAAAA_x")
        web._update(
            tid, status="done",
            items=[
                {"aweme_id": "a1", "url": core.aweme_url("a1"),
                 "status": "failed", "error": "e1"},
                {"aweme_id": "a2", "url": core.aweme_url("a2"),
                 "status": "failed", "error": "e2"},
            ],
            result={"failed": [{"aweme_id": "a1"}, {"aweme_id": "a2"}],
                    "download_count": 0},
        )
        seen = []

        async def fake_list(ids, cfg, on_progress=None, should_stop=None, meta=None):
            seen.extend(ids)
            return {"downloaded": [], "failed": [], "skipped": ids,
                    "download_count": 0, "total_handled": len(ids)}

        with patch.object(config, "load_config", return_value={"cookie": "x=1"}), \
             patch.object(core, "download_aweme_list", side_effect=fake_list):
            resp = web.app.test_client().post(
                f"/api/retry/{tid}", json={"aweme_ids": ["a2"]})
            self.assertEqual(resp.status_code, 200)
            self.wait_finished(tid)
        self.assertEqual(seen, ["a2"])

    def test_config_api_accepts_workers(self):
        client = web.app.test_client()
        with patch.object(config, "update_config", return_value={}) as upd, \
             patch.object(config, "load_config", return_value=dict(config.DEFAULT_CONFIG)):
            resp = client.post("/api/config", json={
                "download_workers": 4,
                "concurrency_mode": "process",
                "max_parallel_tasks": 2,
            })
            self.assertEqual(resp.status_code, 200)
            kwargs = upd.call_args.kwargs
            self.assertEqual(kwargs.get("download_workers"), 4)
            self.assertEqual(kwargs.get("concurrency_mode"), "process")
            self.assertEqual(kwargs.get("max_parallel_tasks"), 2)


class InstallerUpgradeTests(unittest.TestCase):
    def test_installer_has_upgrade_hooks(self):
        iss = (ROOT / "build" / "installer.iss").read_text(encoding="utf-8")
        self.assertIn('AppVersion "1.1.0"', iss)
        # Inno Setup 无 ForceCloseApplications 指令；用 CloseApplications=force
        self.assertIn("CloseApplications=force", iss)
        self.assertNotIn("ForceCloseApplications=", iss)
        self.assertIn("UsePreviousAppDir=yes", iss)
        self.assertIn("PrepareToInstall", iss)
        self.assertIn("KillRunningApp", iss)
        self.assertIn("UninstallOldVersion", iss)
        self.assertIn("/SILENT", iss)
        self.assertIn("taskkill.exe", iss)
        self.assertIn("douyin-tool.exe", iss)

    def test_version_files_synced(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        init = (ROOT / "src" / "douyin_tool" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn('version = "1.1.0"', pyproject)
        self.assertIn('__version__ = "1.1.0"', init)


class ParallelTaskSlotTests(unittest.TestCase):
    def setUp(self):
        with web._TASKS_LOCK:
            web._TASKS.clear()

    def wait_finished(self, tid):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            task = web._get(tid)
            if task["status"] not in ("pending", "running"):
                return task
            time.sleep(0.02)
        raise AssertionError("任务未结束")

    def test_default_still_serializes_tasks(self):
        active = peak = 0

        async def transfer(cb):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1
            return {}

        with patch.object(config, "load_config",
                          return_value={"max_parallel_tasks": 1, "cookie": ""}):
            ids = [web._new_task("one", "test") for _ in range(3)]
            for tid in ids:
                web._run_in_thread(tid, transfer)
            for tid in ids:
                self.assertEqual(self.wait_finished(tid)["status"], "done")
            self.assertEqual(peak, 1)

    def test_parallel_tasks_can_overlap(self):
        active = peak = 0

        async def transfer(cb):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.08)
            active -= 1
            return {}

        with patch.object(config, "load_config",
                          return_value={"max_parallel_tasks": 2, "cookie": ""}):
            ids = [web._new_task("one", "test") for _ in range(4)]
            for tid in ids:
                web._run_in_thread(tid, transfer)
            for tid in ids:
                self.assertEqual(self.wait_finished(tid)["status"], "done")
            self.assertGreaterEqual(peak, 2)
            self.assertLessEqual(peak, 2)


if __name__ == "__main__":
    unittest.main()
