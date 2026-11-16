import ast
import os
import shutil
import tempfile
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_PY_PATH = os.path.join(REPO_ROOT, "app.py")

PLANNER_FUNCS = {
    "_bytes_to_gb_label",
    "_sum_unique_file_footprints",
    "_list_group_cache_safetensors",
    "_h3_image_offload_cache_root",
    "_inventory_h3_image_offload_cache",
    "_disk_free_bytes_for_path",
    "_format_h3_image_manual_cache_cleanup",
    "_plan_minimax_h3_image_offload",
    "_h3_image_group_offload_steps",
    "_log_h3_image_offload_plan",
    "_write_h3_image_offload_policy_marker",
}

# Official-ish measured ceilings used as fallbacks in app.py (GiB, 1024^3).
TE_FALLBACK = int(62.2 * (1024 ** 3))
DIT_FALLBACK = int(61.8 * (1024 ** 3))
RAM_RESERVE = 8 * (1024 ** 3)
DISK_RESERVE = 4 * (1024 ** 3)


def _extract_app_helpers() -> Dict[str, Any]:
    with open(APP_PY_PATH, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=APP_PY_PATH)

    selected = []
    const_assigns = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in PLANNER_FUNCS:
            selected.append(node)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id.startswith("_H3_IMAGE_"):
                    const_assigns.append(node)

    mod = ast.Module(body=const_assigns + selected, type_ignores=[])
    ast.fix_missing_locations(mod)
    ns: Dict[str, Any] = {
        "os": os,
        "shutil": shutil,
        "json": __import__("json"),
        "Any": Any,
        "Dict": Dict,
        "List": List,
        "Optional": Optional,
    }
    exec(compile(mod, APP_PY_PATH, "exec"), ns)
    return ns


def _extract_function_source(name: str) -> str:
    with open(APP_PY_PATH, "r", encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source, filename=APP_PY_PATH)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"function {name} not found")


def _write_file(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


class H3ImageOffloadPlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = _extract_app_helpers()

    def test_hybrid_plan_for_40gb_gpu_80gb_ram(self):
        plan = self.ns["_plan_minimax_h3_image_offload"](
            total_vram_gb=40.0,
            avail_ram_gb=80.0,
            disk_free_bytes=80 * (1024 ** 3),
            te_weight_logical_bytes=62 * (1024 ** 3),
            dit_weight_logical_bytes=62 * (1024 ** 3),
            te_weight_measured=True,
            dit_weight_measured=True,
            offload_root="/content/models/_offload_cache/minimax_h3_image",
        )
        self.assertEqual(plan["mode"], "hybrid_encoder_disk_dit_cpu")
        self.assertTrue(plan["te_offload_to_disk"])
        self.assertFalse(plan["dit_offload_to_disk"])
        self.assertFalse(plan["vae_offload_to_disk"])
        self.assertFalse(plan["te_use_stream"])
        self.assertFalse(plan["dit_use_stream"])
        self.assertFalse(plan["vae_use_stream"])
        self.assertTrue(plan["low_cpu_mem_usage"])
        steps = self.ns["_h3_image_group_offload_steps"](plan, "/tmp/offload/minimax_h3_image")
        self.assertEqual([s["component"] for s in steps], ["text_encoder", "transformer", "vae"])
        self.assertEqual(steps[0]["offload_to_disk_path"], "/tmp/offload/minimax_h3_image/text_encoder")
        self.assertIsNone(steps[1]["offload_to_disk_path"])
        self.assertIsNone(steps[2]["offload_to_disk_path"])
        self.assertTrue(steps[2]["enable_tiling"])
        self.assertIn("shard", plan["estimates"]["te_weight_source"])
        self.assertIn("不是", plan["estimates"]["peak_disclaimer"])

    def test_high_resource_components_manager_path_unchanged(self):
        plan = self.ns["_plan_minimax_h3_image_offload"](
            total_vram_gb=80.0,
            avail_ram_gb=167.0,
            disk_free_bytes=1,  # must not fail even if disk is nearly full
            te_weight_logical_bytes=62 * (1024 ** 3),
            dit_weight_logical_bytes=62 * (1024 ** 3),
            te_weight_measured=True,
            dit_weight_measured=True,
        )
        self.assertEqual(plan["mode"], "components_manager")
        self.assertFalse(plan["te_offload_to_disk"])
        self.assertFalse(plan["dit_offload_to_disk"])

        # High-RAM, mid-VRAM still uses the pre-existing CPU group-offload (both in RAM, streamed).
        mid = self.ns["_plan_minimax_h3_image_offload"](
            total_vram_gb=40.0,
            avail_ram_gb=132.0,
            disk_free_bytes=1,
        )
        self.assertEqual(mid["mode"], "cpu_group_offload")
        self.assertFalse(mid["te_offload_to_disk"])
        self.assertFalse(mid["dit_offload_to_disk"])
        self.assertTrue(mid["te_use_stream"])
        self.assertTrue(mid["dit_use_stream"])
        self.assertFalse(mid["vae_use_stream"])

    def test_disk_shortfall_fails_early_and_does_not_delete(self):
        offload_root = "/content/models/_offload_cache/minimax_h3_image"
        weights_hint = "/content/models/MiniMaxAI/MiniMax-H3-Image"
        with self.assertRaises(RuntimeError) as ctx:
            self.ns["_plan_minimax_h3_image_offload"](
                total_vram_gb=40.0,
                avail_ram_gb=80.0,
                disk_free_bytes=24 * 1024 * 1024,  # ~24 MiB, like a full Colab disk
                te_weight_logical_bytes=62 * (1024 ** 3),
                dit_weight_logical_bytes=62 * (1024 ** 3),
                te_weight_measured=True,
                dit_weight_measured=True,
                existing_te_cache_allocated_bytes=62 * (1024 ** 3),
                existing_dit_cache_allocated_bytes=46 * (1024 ** 3),
                offload_root=offload_root,
                official_weights_hint=weights_hint,
                cache_inventory={
                    "root_is_symlink": False,
                    "text_encoder": {"allocated_bytes": 62 * (1024 ** 3), "is_symlink": False},
                    "transformer": {"allocated_bytes": 46 * (1024 ** 3), "is_symlink": False},
                },
            )
        msg = str(ctx.exception)
        self.assertIn("提前停止", msg)
        self.assertIn("不会自动删除", msg)
        self.assertIn(weights_hint, msg)
        self.assertIn(f"rm -rf {offload_root}", msg)
        self.assertIn("ls -ld", msg)
        self.assertIn("请勿删除官方权重", msg)
        self.assertNotIn("os.remove", msg)
        self.assertNotIn("rmtree", msg)

        # The planner itself must not call any delete API.
        with mock.patch("os.remove") as rem, mock.patch("shutil.rmtree") as rmt, mock.patch("os.unlink") as unl:
            with self.assertRaises(RuntimeError):
                self.ns["_plan_minimax_h3_image_offload"](
                    total_vram_gb=40.0,
                    avail_ram_gb=80.0,
                    disk_free_bytes=1024,
                    te_weight_logical_bytes=TE_FALLBACK,
                    dit_weight_logical_bytes=DIT_FALLBACK,
                    offload_root=offload_root,
                    official_weights_hint=weights_hint,
                )
            rem.assert_not_called()
            rmt.assert_not_called()
            unl.assert_not_called()

    def test_low_ram_is_rejected_before_load(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.ns["_plan_minimax_h3_image_offload"](
                total_vram_gb=40.0,
                avail_ram_gb=40.0,
                disk_free_bytes=80 * (1024 ** 3),
                te_weight_logical_bytes=62 * (1024 ** 3),
                dit_weight_logical_bytes=62 * (1024 ** 3),
                te_weight_measured=True,
                dit_weight_measured=True,
                offload_root="/tmp/offload/minimax_h3_image",
            )
        msg = str(ctx.exception)
        self.assertIn("可用内存不足", msg)
        self.assertIn("不会自动删除", msg)
        self.assertIn("不是实测推理峰值", msg)

        # Just below DiT + reserve should also reject (62 + 8 = 70 GiB).
        with self.assertRaises(RuntimeError):
            self.ns["_plan_minimax_h3_image_offload"](
                total_vram_gb=40.0,
                avail_ram_gb=69.0,
                disk_free_bytes=80 * (1024 ** 3),
                te_weight_logical_bytes=62 * (1024 ** 3),
                dit_weight_logical_bytes=62 * (1024 ** 3),
                te_weight_measured=True,
                dit_weight_measured=True,
            )

        # 70 GiB available meets 62 + 8 reserve and should plan hybrid.
        plan = self.ns["_plan_minimax_h3_image_offload"](
            total_vram_gb=40.0,
            avail_ram_gb=70.0,
            disk_free_bytes=80 * (1024 ** 3),
            te_weight_logical_bytes=62 * (1024 ** 3),
            dit_weight_logical_bytes=62 * (1024 ** 3),
            te_weight_measured=True,
            dit_weight_measured=True,
        )
        self.assertEqual(plan["mode"], "hybrid_encoder_disk_dit_cpu")

    def test_inference_function_and_call_order_unchanged_in_source(self):
        infer = _extract_function_source("run_minimax_h3_image_inference")
        self.assertIn("ref_images", infer)
        self.assertIn('call_kw["image"]', infer)
        self.assertIn('output": "images"', infer)
        # Docstring still forbids overriding the fixed 6-step / CFG-distilled call.
        self.assertIn("严禁传入 num_inference_steps / guidance_scale / negative_prompt", infer)
        # The actual call kwargs must not pass those parameters.
        body = infer.split('"""', 2)[-1]
        self.assertNotIn("num_inference_steps", body)
        self.assertNotIn("guidance_scale", body)
        self.assertNotIn("negative_prompt", body)

        load = _extract_function_source("load_minimax_h3_image_pipeline")
        self.assertIn("total_vram_gb >= 65.0 and avail_ram_gb >= 132.0", load)
        self.assertIn("ComponentsManager", load)
        self.assertIn("manager.enable_auto_cpu_offload", load)
        self.assertIn("_plan_minimax_h3_image_offload", load)
        self.assertIn("_h3_image_group_offload_steps", load)
        # Mid-path still loads in official component order.
        self.assertLess(load.index('names=["scheduler", "tokenizer", "processor"]'), load.index("_h3_image_group_offload_steps"))
        # High-resource path returns before the hybrid planner is used.
        high_return = load.index("return pipe, mode_label")
        plan_call = load.index("_plan_minimax_h3_image_offload")
        self.assertLess(high_return, plan_call)
        # Must not automatically delete caches or weights.
        self.assertNotIn("rmtree", load)
        self.assertNotIn("os.remove", load)
        self.assertNotIn("os.unlink(offload", load)

    def test_unique_inode_and_symlink_and_sparse_measurement(self):
        sum_fn = self.ns["_sum_unique_file_footprints"]
        list_fn = self.ns["_list_group_cache_safetensors"]
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real.safetensors")
            _write_file(real, b"abcdefgh" * 1024)
            link = os.path.join(tmp, "alias.safetensors")
            os.symlink(real, link)
            footprints = sum_fn([real, link])
            self.assertEqual(footprints["file_count"], 1)
            self.assertEqual(footprints["logical_bytes"], 8 * 1024)

            # Directory symlink must not be scanned (would otherwise see official-like shards).
            official = os.path.join(tmp, "official")
            os.makedirs(official)
            _write_file(os.path.join(official, "group_should_not_count_if_dir_linked.safetensors"), b"x" * 100)
            # actually if we scan official directly it would count; the protection is skipping when cache_dir is a symlink
            cache_link = os.path.join(tmp, "cache_link")
            os.symlink(official, cache_link)
            skipped = list_fn(cache_link)
            self.assertTrue(skipped["is_symlink"])
            self.assertEqual(skipped["paths"], [])
            self.assertEqual(skipped["file_count"], 0)

            cache = os.path.join(tmp, "cache")
            os.makedirs(cache)
            _write_file(os.path.join(cache, "group_aaa.safetensors"), b"g" * 2048)
            _write_file(os.path.join(cache, "model-00001-of-00014.safetensors"), b"official-shard" * 100)
            nested = os.path.join(cache, "nested")
            os.makedirs(nested)
            _write_file(os.path.join(nested, "group_nested.safetensors"), b"nope")
            dir_link = os.path.join(cache, "outside")
            os.symlink(official, dir_link)
            scanned = list_fn(cache)
            self.assertEqual(scanned["file_count"], 1)
            self.assertTrue(scanned["paths"][0].endswith("group_aaa.safetensors"))

            sparse = os.path.join(tmp, "sparse.safetensors")
            with open(sparse, "wb") as f:
                f.truncate(10 * 1024 * 1024)
            sparse_fp = sum_fn([sparse])
            self.assertEqual(sparse_fp["logical_bytes"], 10 * 1024 * 1024)
            self.assertLess(sparse_fp["allocated_bytes"], sparse_fp["logical_bytes"])
            self.assertLessEqual(sparse_fp["allocated_bytes"], 64 * 1024)

    def test_policy_marker_is_small_and_skips_symlink_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "offload")
            plan = {"mode": "hybrid_encoder_disk_dit_cpu", "te_offload_to_disk": True}
            self.ns["_write_h3_image_offload_policy_marker"](root, plan)
            marker = os.path.join(root, "easycolab_offload_policy.json")
            self.assertTrue(os.path.isfile(marker))
            self.assertLess(os.path.getsize(marker), 1024)

            link_root = os.path.join(tmp, "link_root")
            target = os.path.join(tmp, "secret_weights")
            os.makedirs(target)
            os.symlink(target, link_root)
            self.ns["_write_h3_image_offload_policy_marker"](link_root, plan)
            self.assertFalse(os.path.exists(os.path.join(target, "easycolab_offload_policy.json")))
            self.assertFalse(os.path.exists(os.path.join(link_root, "easycolab_offload_policy.json")))

    def test_fallback_constants_are_labeled_ceilings_not_peaks(self):
        self.assertEqual(self.ns["_H3_IMAGE_FALLBACK_TE_WEIGHT_BYTES"], TE_FALLBACK)
        self.assertEqual(self.ns["_H3_IMAGE_FALLBACK_DIT_WEIGHT_BYTES"], DIT_FALLBACK)
        self.assertEqual(self.ns["_H3_IMAGE_RAM_OVERHEAD_RESERVE_BYTES"], RAM_RESERVE)
        self.assertEqual(self.ns["_H3_IMAGE_DISK_WRITE_RESERVE_BYTES"], DISK_RESERVE)
        # Hybrid disk requirement uses TE logical size + write reserve, not two 62GiB caches.
        with self.assertRaises(RuntimeError) as ctx:
            self.ns["_plan_minimax_h3_image_offload"](
                total_vram_gb=40.0,
                avail_ram_gb=80.0,
                disk_free_bytes=TE_FALLBACK + DISK_RESERVE - 1,
            )
        self.assertIn(_bytes_hint(TE_FALLBACK + DISK_RESERVE), str(ctx.exception).replace("GiB", "GiB"))


def _bytes_hint(num: int) -> str:
    return f"{(num / (1024 ** 3)):.2f} GiB"


if __name__ == "__main__":
    unittest.main()
