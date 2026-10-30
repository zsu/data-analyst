"""scan_secrets: the 32-hex rule for key-echoing sources applies everywhere except other sources' own data."""
import os
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT = os.path.join(ROOT, "plugins", "data-analyst", "skills", "data-analyst", "scripts", "report")
sys.path.insert(0, REPORT)
import scan_secrets  # noqa: E402

HEX = "0123456789abcdef0123456789abcdef"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class TestHex32Scope(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = self.tmp.name
        write(os.path.join(self.out, "data", "bls", "series.csv"), "series_id,value\nCUUR0000SA0,1\n")

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self):
        return scan_secrets.scan_tree([self.out], [], hex32=True)[0]

    def test_ids_in_a_non_echoing_source_are_not_keys(self):
        write(os.path.join(self.out, "data", "usgs", "sites.csv"), f"id,station\nsite_{HEX},A\n")
        self.assertEqual(self.scan(), [])

    def test_hex_in_the_answer_or_the_echoing_source_still_fails(self):
        write(os.path.join(self.out, "answer.md"), f"The request used {HEX}.\n")
        write(os.path.join(self.out, "data", "bls", "errors.txt"), f"key {HEX} invalid\n")
        hits = self.scan()
        self.assertTrue(any("answer.md" in h for h in hits), hits)
        self.assertTrue(any("errors.txt" in h for h in hits), hits)

    def test_pdf_document_ids_are_not_keys(self):
        # a saved agency PDF ends with /ID[<32 hex><32 hex>]; a real key value in it still counts
        pdf = os.path.join(self.out, "published", "raw", "statement.pdf")
        os.makedirs(os.path.dirname(pdf))
        with open(pdf, "wb") as f:
            f.write(b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n<</ID[<" + HEX.upper().encode() + b"><"
                    + HEX.encode() + b">]>>\n%%EOF\n")
        self.assertEqual(self.scan(), [])
        self.assertTrue(scan_secrets.scan_tree([self.out], [HEX.encode()], hex32=True)[0])

    def test_saved_reference_pages_are_not_keys(self):
        # a page fetch.py --cite saved holds cache hashes and script ids; a key-shaped URL or a real key still counts
        page = os.path.join(self.out, "references", "nber2008.html")
        write(page, f'<script src="https://cdn-cookieyes.com/client_data/{HEX}/script.js"></script>\n')
        self.assertEqual(self.scan(), [])
        self.assertTrue(scan_secrets.scan_tree([self.out], [HEX.encode()], hex32=True)[0])
        write(page, f'<a href="https://api.eia.gov/v2/x?api_key={HEX}">x</a>\n')
        self.assertTrue(self.scan())
        write(os.path.join(self.out, "notes", "references", "x.txt"), f"token {HEX}\n")   # only the top folder
        self.assertTrue(any("notes" in h for h in self.scan()))

    def test_zip_members_follow_the_same_rule(self):
        z = os.path.join(self.out, "replication_package.zip")
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("data/usgs/sites.csv", f"id\nsite_{HEX}\n")
            f.writestr("notes.txt", f"token {HEX}\n")
        hits = self.scan()
        self.assertFalse(any("data/usgs" in h for h in hits), hits)
        self.assertTrue(any("notes.txt" in h for h in hits), hits)

    def test_a_literal_key_is_caught_anywhere(self):
        key = "zz" + HEX[:30]
        write(os.path.join(self.out, "data", "usgs", "sites.csv"), f"id\n{key}\n")
        hits = scan_secrets.scan_tree([self.out], [key.encode()], hex32=True)[0]
        self.assertTrue(hits)


if __name__ == "__main__":
    unittest.main()
