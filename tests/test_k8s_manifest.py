"""The shipped egress policy must actually reach cluster DNS.

v0.5.0 in a Cilium cluster: the policy allowed DNS only through `ipBlock`, but Cilium
matches a destination inside the cluster by identity and never by CIDR, so every
lookup from the pod was dropped and the poller logged `deskmonitor pr exited 2` on
every cycle. CI streams this directory's parent `k8s/` next to the tests.
"""
from pathlib import Path
import unittest

import yaml

MANIFEST = Path(__file__).resolve().parent.parent / "k8s" / "pr-agent.yaml"


class EgressPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        documents = yaml.safe_load_all(MANIFEST.read_text())
        cls.policy = next(d for d in documents if d and d.get("kind") == "NetworkPolicy")

    def dns_rules(self):
        return [rule for rule in self.policy["spec"]["egress"]
                if {"53"} <= {str(port["port"]) for port in rule.get("ports", [])}]

    def test_cluster_dns_pods_are_selected_by_namespace_and_label(self):
        selected = [to for rule in self.dns_rules() for to in rule["to"]
                    if "namespaceSelector" in to and "podSelector" in to]
        self.assertEqual([{key: value["matchLabels"] for key, value in to.items()} for to in selected], [{
            "namespaceSelector": {"kubernetes.io/metadata.name": "kube-system"},
            "podSelector": {"k8s-app": "kube-dns"}}],
            "CoreDNS is an in-cluster identity; an ipBlock rule never reaches it on Cilium")

    def test_the_selector_rule_covers_udp_and_tcp(self):
        rule = next(rule for rule in self.dns_rules() if any("namespaceSelector" in to for to in rule["to"]))
        self.assertEqual({(port["protocol"], port["port"]) for port in rule["ports"]}, {("UDP", 53), ("TCP", 53)})

    def test_the_cidr_rule_stays_for_resolvers_a_selector_cannot_name(self):
        self.assertTrue(any("ipBlock" in to for rule in self.dns_rules() for to in rule["to"]),
                        "NodeLocal DNSCache and a pre-DNAT kube-dns Service IP are only reachable by CIDR")

    def test_everything_else_stays_limited_to_the_public_internet(self):
        rest = [rule for rule in self.policy["spec"]["egress"] if rule not in self.dns_rules()]
        self.assertEqual(len(rest), 1)
        self.assertEqual(rest[0]["to"][0]["ipBlock"]["except"],
                         ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16"])
        self.assertNotIn("ports", rest[0])
