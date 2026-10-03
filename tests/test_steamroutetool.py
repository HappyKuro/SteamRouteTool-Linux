import contextlib
import io
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import steamroutetool as srt  # noqa: E402

SAMPLE_CONFIG = {
    "revision": 1,
    "pops": {
        "fra": {"desc": "Frankfurt (Germany)", "relays": [
            {"ipv4": "155.133.226.1", "port_range": [27015, 27060]},
            {"ipv4": "155.133.226.2", "port_range": [27015, 27060]},
            {"ipv4": "155.133.226.2", "port_range": [27015, 27060]},  # duplicate
        ]},
        "ord": {"desc": "Chicago (Illinois)", "relays": [
            {"ipv4": "155.133.249.1", "port_range": [27015, 27060]},
        ]},
        "sto": {"desc": "Stockholm - Kista (Sweden)", "relays": [
            {"ipv4": "not-an-ip"},
            {"ipv4": "146.66.157.1"},
        ]},
        "tst": {"desc": "cloud-test pop", "relays": [{"ipv4": "10.0.0.1"}]},
        "bad name": {"desc": "Injection", "relays": [{"ipv4": "10.0.0.2"}]},
        "norelays": {"desc": "Empty"},
        "empty": {"desc": "Empty relays", "relays": []},
    },
}


def routes():
    return srt.parse_routes(SAMPLE_CONFIG)


class ParseRoutesTest(unittest.TestCase):
    def test_filters_and_sorts(self):
        rs = routes()
        self.assertEqual([r.name for r in rs], ["ord", "fra", "sto"])

    def test_dedupes_and_validates_relays(self):
        by_name = {r.name: r for r in routes()}
        self.assertEqual([ip for ip, _ in by_name["fra"].relays], ["155.133.226.1", "155.133.226.2"])
        self.assertEqual([ip for ip, _ in by_name["sto"].relays], ["146.66.157.1"])

    def test_bad_shape(self):
        with self.assertRaises(ValueError):
            srt.parse_routes({"nope": 1})
        with self.assertRaises(ValueError):
            srt.parse_routes([])


class RouteStateTest(unittest.TestCase):
    def test_block_state(self):
        fra = {r.name: r for r in routes()}["fra"]
        self.assertEqual(srt.route_block_state(fra, set()), "none")
        self.assertEqual(srt.route_block_state(fra, {("fra", "155.133.226.1")}), "some")
        self.assertEqual(srt.route_block_state(fra, fra.keys), "all")
        # same IP under a different pop name doesn't count
        self.assertEqual(srt.route_block_state(fra, {("ord", "155.133.226.1")}), "none")

    def test_sort_by_ping_puts_unreachable_last(self):
        rs = routes()
        by_name = {r.name: r for r in rs}
        by_name["ord"].ping = {"155.133.249.1": 120.0}
        by_name["fra"].ping = {"155.133.226.1": 20.0, "155.133.226.2": None}
        self.assertEqual([r.name for r in srt.sort_routes(rs, "ping")], ["fra", "ord", "sto"])

    def test_find_routes(self):
        found, missing = srt.find_routes(routes(), ["FRA", "chicago (illinois)", "fra", "xyz"])
        self.assertEqual([r.name for r in found], ["fra", "ord"])
        self.assertEqual(missing, ["xyz"])


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self.view = routes()  # ord, fra, sto

    def test_numbers_ranges_and_relays(self):
        pops, relays = srt.parse_selection("1, 3-2 2.2", self.view)
        self.assertEqual([r.name for r in pops], ["ord", "fra", "sto"])
        self.assertEqual([(r.name, ip) for r, ip in relays], [("fra", "155.133.226.2")])

    def test_errors(self):
        for bad in ["", "0", "4", "1-9", "2.3", "x", "1..2"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                srt.parse_selection(bad, self.view)


class RulesTest(unittest.TestCase):
    LISTING = "\n".join([
        f"-N {srt.CHAIN}",
        f'-A {srt.CHAIN} -d 155.133.226.1/32 -p udp -m udp --dport 27015:27202 '
        f'-m comment --comment "SteamRouteTool:fra:155.133.226.1" -j DROP',
        f'-A {srt.CHAIN} -d 155.133.226.1/32 -p icmp '
        f'-m comment --comment "SteamRouteTool:fra:155.133.226.1" -j DROP',
        f"-A {srt.CHAIN} -d 1.1.1.1/32 -j DROP",  # not ours
        "-A OUTPUT -j DROP",
    ])

    def test_parse_rules(self):
        rules = srt.parse_rules(self.LISTING)
        self.assertEqual(list(rules), [("fra", "155.133.226.1")])
        self.assertEqual(len(rules[("fra", "155.133.226.1")]), 2)

    def test_parse_rules_unquoted_comment(self):
        line = f"-A {srt.CHAIN} -d 1.2.3.4/32 -p icmp -m comment --comment SteamRouteTool:ams:1.2.3.4 -j DROP"
        self.assertEqual(list(srt.parse_rules(line)), [("ams", "1.2.3.4")])

    def test_restore_script_reblock_removes_old_rules_first(self):
        script = srt.build_restore_script(srt.parse_rules(self.LISTING),
                                          block={("fra", "155.133.226.1")}, unblock=set())
        lines = script.splitlines()
        self.assertEqual(lines[0], "*filter")
        self.assertEqual(lines[-1], "COMMIT")
        self.assertTrue(lines[1].startswith(f"-D {srt.CHAIN} -d 155.133.226.1/32 -p udp"))
        self.assertTrue(lines[2].startswith(f"-D {srt.CHAIN} -d 155.133.226.1/32 -p icmp"))
        self.assertEqual(len([l for l in lines if l.startswith("-A ")]), 3)

    def test_restore_script_unblock_and_noop(self):
        rules = srt.parse_rules(self.LISTING)
        script = srt.build_restore_script(rules, block=set(), unblock={("fra", "155.133.226.1")})
        self.assertEqual(len([l for l in script.splitlines() if l.startswith("-D ")]), 2)
        self.assertIsNone(srt.build_restore_script(rules, block=set(), unblock={("ord", "9.9.9.9")}))


def iptables_usable():
    if os.environ.get("SRT_IPTABLES_TESTS") != "1":
        return False, "set SRT_IPTABLES_TESTS=1 (as root) to run real iptables tests"
    if os.geteuid() != 0 or not shutil.which("iptables-restore"):
        return False, "needs root and iptables"
    if subprocess.run(["iptables", "-S", srt.CHAIN], capture_output=True).returncode == 0:
        return False, f"{srt.CHAIN} chain already exists; refusing to touch real rules"
    return True, ""


_ok, _why = iptables_usable()


@unittest.skipUnless(_ok, _why)
class IptablesIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.fw = srt.Firewall()
        self.rs = {r.name: r for r in routes()}

    def tearDown(self):
        self.fw.clear()

    def test_block_unblock_only_clear(self):
        fra, ord_, sto = self.rs["fra"], self.rs["ord"], self.rs["sto"]
        self.fw.apply(block=fra.keys)
        self.assertEqual(self.fw.blocked(), fra.keys)

        # re-blocking (incl. a partial overlap) must not duplicate rules
        self.fw.apply(block=fra.keys | ord_.keys)
        self.assertEqual(self.fw.blocked(), fra.keys | ord_.keys)
        rules = srt.parse_rules(self.fw._listing())
        self.assertTrue(all(len(v) == 3 for v in rules.values()), rules)

        self.fw.apply(unblock=fra.keys)
        self.assertEqual(self.fw.blocked(), ord_.keys)

        everything = fra.keys | ord_.keys | sto.keys
        self.fw.apply(block=everything - sto.keys, unblock=sto.keys)
        self.assertEqual(self.fw.blocked(), fra.keys | ord_.keys)

        self.fw.clear()
        self.assertEqual(self.fw.blocked(), set())
        self.assertFalse(self.fw._jump_exists())

    def test_failed_batch_applies_nothing(self):
        self.fw.apply(block=self.rs["ord"].keys)
        before = self.fw._listing()
        orig = srt.build_restore_script

        def broken(*a, **kw):
            return orig(*a, **kw).replace("COMMIT", f"-D {srt.CHAIN} -d 203.0.113.9 -j DROP\nCOMMIT")
        srt.build_restore_script = broken
        try:
            with self.assertRaises(srt.FirewallError):
                self.fw.apply(block=self.rs["fra"].keys)
        finally:
            srt.build_restore_script = orig
        self.assertEqual(self.fw._listing(), before)

    def test_dry_run_changes_nothing(self):
        dry = srt.Firewall(dry_run=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            dry.apply(block=self.rs["fra"].keys)
        self.assertIn("iptables-restore", out.getvalue())
        self.assertEqual(dry.blocked(), self.rs["fra"].keys)  # simulated
        self.assertEqual(self.fw.blocked(), set())            # real


if __name__ == "__main__":
    unittest.main()
