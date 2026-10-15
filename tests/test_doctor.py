"""运行环境自检（doctor）：检查项、缺失判定、安装命令。"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docseek import doctor                              # noqa: E402


class ChecksShapeTest(unittest.TestCase):
    def test_every_check_has_the_fields_we_render(self):
        results = doctor.run_checks()
        self.assertGreaterEqual(len(results), 10)
        for item in results:
            for key in ("key", "label", "ok", "detail", "impact", "package",
                        "required"):
                self.assertIn(key, item, "检查项缺字段 %s：%s" % (key, item))
            self.assertIsInstance(item["ok"], bool)
            self.assertTrue(item["impact"], "每项都要说明缺了会影响什么")
            if item["key"] != "fts5":
                self.assertTrue(item["package"], "可安装的项必须有包名")

    def test_required_items_are_the_ones_that_block_startup(self):
        required = {r["key"] for r in doctor.run_checks() if r["required"]}
        self.assertEqual(required, {"gi", "gtk", "fts5", "poppler"},
                         "必需项应当只有启动/检索这类硬依赖")


class MissingDetectionTest(unittest.TestCase):
    def _fake(self, present):
        """构造一个 run_checks 结果：present 里的键标记为可用。"""
        results = []
        for item in doctor.run_checks():
            item = dict(item)
            item["ok"] = item["key"] in present
            item["detail"] = "" if item["ok"] else "测试模拟缺失"
            results.append(item)
        return results

    def test_all_present_means_no_command(self):
        present = {r["key"] for r in doctor.run_checks()}
        results = self._fake(present)
        self.assertEqual(doctor.missing_packages(results), [])
        self.assertIsNone(doctor.apt_command(results))
        self.assertIn("依赖齐备", doctor.format_report(results))

    def test_missing_required_blocks_and_is_reported(self):
        results = self._fake({"gi", "gtk", "fts5"})       # 缺 poppler
        missing = doctor.missing_packages(results)
        self.assertIn("poppler-utils", missing)
        self.assertTrue(doctor.missing_required(results))
        command = doctor.apt_command(results)
        self.assertTrue(command.startswith("sudo apt install -y"))
        self.assertIn("poppler-utils", command)
        report = doctor.format_report(results)
        self.assertIn("必需依赖缺失", report)
        self.assertIn("sudo apt install -y", report)

    def test_optional_missing_does_not_block(self):
        present = {r["key"] for r in doctor.run_checks()} - {"catdoc", "wps2text"}
        results = self._fake(present)
        self.assertEqual(doctor.missing_required(results), [])
        self.assertIn("catdoc", doctor.apt_command(results))
        self.assertIn("可选项", doctor.format_report(results))

    def test_required_packages_come_first(self):
        results = self._fake({"gi", "gtk"})               # 缺 fts5 与 poppler（必需）
        missing = doctor.missing_packages(results)
        self.assertEqual(missing[0], "poppler-utils")

    def test_real_which_probe(self):
        """真的把 which 掐掉，验证检测链路（不是只测假数据）。"""
        with mock.patch.object(doctor.shutil, "which", return_value=None):
            results = doctor.run_checks()
        by_key = {r["key"]: r for r in results}
        self.assertFalse(by_key["poppler"]["ok"])
        self.assertFalse(by_key["tesseract"]["ok"])
        self.assertIn("缺：", by_key["poppler"]["detail"])
        self.assertIn("poppler-utils", doctor.apt_command(results))


class DoctorCommandTest(unittest.TestCase):
    def test_doctor_is_a_registered_subcommand(self):
        from docseek import cli
        args = cli.build_parser().parse_args(["doctor", "--json"])
        self.assertIs(args.func, cli.cmd_doctor)
        self.assertTrue(args.json)

    def test_cmd_doctor_prints_report(self):
        import contextlib
        import io

        from docseek import cli
        args = cli.build_parser().parse_args(["doctor"])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.cmd_doctor(args)
        text = buffer.getvalue()
        self.assertIn("运行环境自检", text)
        expected = 1 if doctor.missing_required() else 0
        self.assertEqual(code, expected)
        if expected == 0:
            self.assertIn("依赖齐备", text)

    def test_cli_doctor_really_runs_in_a_subprocess(self):
        """真起一个进程跑命令行：验证 doctor 能独立启动、JSON 可解析。

        用子进程而不是在测试进程里调 main()，因为 main() 会 install() 子进程登记
        并做收尾terminate，同进程内跑会把测试进程的后续子进程全带走。
        """
        import json
        import subprocess

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ, PYTHONPATH=root)
        proc = subprocess.run(
            [sys.executable, "-m", "docseek.cli", "doctor", "--json"],
            cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=300)
        data = json.loads(proc.stdout.decode("utf-8"))
        self.assertIn("checks", data)
        self.assertIn("command", data)
        self.assertTrue(data["checks"])
        expected = 1 if doctor.missing_required() else 0
        self.assertEqual(proc.returncode, expected)


if __name__ == "__main__":
    unittest.main()
