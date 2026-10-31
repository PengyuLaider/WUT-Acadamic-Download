"""Offline regression tests: all external HTTP and resolver operations are mocked."""
import argparse
import contextlib
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import paper_oa as oa
import pdf_check
import webvpn_url as vpn

TITLE = 'A reliable campus paper download'
DOI = '10.1234/example(2025)'

def pdf_fixture(title=TITLE, doi=DOI, single=False):
    escaped = doi.replace('(', r'\(').replace(')', r'\)')
    stream = f'BT /F1 12 Tf 40 740 Td ({title}) Tj 0 -20 Td ({escaped}) Tj ET'.encode()
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>' if single else b'<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        f'<< /Length {len(stream)} >>\nstream\n'.encode() + stream + b'\nendstream']
    if not single:
        objects.append(objects[2])
    body = b'%PDF-1.4\n'; offsets = [0]
    for n, obj in enumerate(objects, 1):
        offsets.append(len(body)); body += f'{n} 0 obj\n'.encode() + obj + b'\nendobj\n'
    start = len(body)
    body += f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode()
    for offset in offsets[1:]: body += f'{offset:010d} 00000 n \n'.encode()
    return body + f'trailer\n<< /Root 1 0 R /Size {len(offsets)} >>\nstartxref\n{start}\n%%EOF\n'.encode()


class Offline(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = oa.load_config(None)

    def response(self, body, url='https://example.org/paper', status=200):
        return dict(status=status, body=body, headers={}, url=url)

    def seed(self, body=None):
        path = self.root / 'paper.pdf'; path.write_bytes(body or pdf_fixture())
        record = dict(doi=DOI, identifier=DOI, title=TITLE, path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        manifest = self.root / 'manifest.jsonl'
        manifest.write_text(json.dumps(record) + '\n', encoding='utf-8')
        return path, manifest, record

    def test_balanced_doi(self):
        self.assertEqual(oa.normalize_doi('https://doi.org/' + DOI), DOI)
        self.assertEqual(oa.normalize_doi('(' + DOI + ')'), None)
        self.assertEqual(oa.normalize_doi(DOI + ').'), DOI)

    def test_invalid_identifiers(self):
        for value in ['arxiv:garbage', 'pmid:abc', 'pmcid:x123', 'javascript:alert(1)']:
            with self.subTest(value=value): self.assertEqual(oa.classify_identifier(value)[0], 'unknown')

    def test_arxiv_valid(self):
        self.assertEqual(oa.classify_identifier('arxiv:1706.03762v2'), ('arxiv','1706.03762v2'))

    def test_explicit_missing_config(self):
        with self.assertRaises(ValueError): oa.load_config(str(self.root / 'missing.json'))

    def test_bad_config(self):
        p = self.root / 'config.json'; p.write_text('{')
        with self.assertRaises(ValueError): oa.load_config(str(p))

    def test_config_requires_object(self):
        p = self.root / 'config.json'; p.write_text('[]')
        with self.assertRaises(ValueError): oa.load_config(str(p))

    def test_nonfinite_config_rejected(self):
        p = self.root / 'config.json'; p.write_text('{"max_pdf_mb": NaN}')
        with self.assertRaises(ValueError): oa.load_config(str(p))

    def test_redirect_option_honored(self):
        from unittest.mock import MagicMock
        response=MagicMock();response.__enter__.return_value=response
        response.status=200;response.geturl.return_value='https://e.org/x';response.headers={};response.read.return_value=b'{}'
        with patch.object(oa.request,'build_opener') as build, patch.object(oa.request,'urlopen',side_effect=AssertionError('redirects enabled')):
            build.return_value.open.return_value=response
            self.assertEqual(oa.http_get('https://e.org/x',self.cfg,allow_redirects=False)['status'],200)
            self.assertEqual(type(build.call_args.args[0]).__name__,'NoRedirect')

    def test_arxiv_rate_limit(self):
        from unittest.mock import MagicMock
        response=MagicMock();response.__enter__.return_value=response
        response.status=200;response.geturl.return_value='https://export.arxiv.org';response.headers={};response.read.return_value=b'{}'
        with patch.object(oa.request,'urlopen',return_value=response),patch.object(oa.time,'monotonic',return_value=100),patch.object(oa.time,'sleep') as sleep:
            oa.http_get('https://export.arxiv.org/api/query',self.cfg)
            oa.http_get('https://export.arxiv.org/api/query',self.cfg)
            sleep.assert_called_once_with(3.0)

    def test_429_long_wait_not_retried(self):
        from urllib.error import HTTPError
        failure=HTTPError('https://e.org/x',429,'Too many requests',{'Retry-After':'300'},io.BytesIO(b'{}'))
        with patch.object(oa.request,'urlopen',side_effect=failure) as get, patch.object(oa.time,'sleep') as sleep:
            self.assertEqual(oa.http_get('https://e.org/x',self.cfg)['status'],429)
            self.assertEqual(get.call_count,1);sleep.assert_not_called()

    def test_invalid_limit_config(self):
        p = self.root / 'config.json'; p.write_text('{"max_pdf_mb": 0}')
        with self.assertRaises(ValueError): oa.load_config(str(p))

    def test_reject_html_and_truncation(self):
        for body in [b'<html>login</html>', pdf_fixture()[:-15], b'%PDF-1.4\nbroken\n%%EOF']:
            with self.subTest(body=body[:20]): self.assertFalse(pdf_check.check_bytes(body)['valid'])

    def test_pdf_identity(self):
        checked = pdf_check.check_bytes(pdf_fixture(), DOI, TITLE)
        self.assertIs(checked['valid'], True)
        self.assertEqual(checked['identity'], 'matched')

    def test_single_page_requires_review(self):
        checked = pdf_check.check_bytes(pdf_fixture(single=True), DOI, TITLE)
        self.assertTrue(checked['valid']); self.assertEqual(checked['identity'], 'matched')
        self.assertFalse(pdf_check.verified(checked)); self.assertIn('single_page_confirm_full_text', checked['review_required'])

    def test_wrong_paper_pending(self):
        checked = pdf_check.check_bytes(pdf_fixture('Another unrelated paper'), DOI, TITLE)
        self.assertTrue(checked['valid']); self.assertEqual(checked['identity'], 'unverified')

    def test_no_parser_pending(self):
        with patch.dict(sys.modules, {'pypdf': None}): checked = pdf_check.check_bytes(pdf_fixture(), DOI, TITLE)
        self.assertIsNone(checked['valid']); self.assertEqual(checked['identity'], 'unverified')

    def test_big_initial_pdf(self):
        self.cfg['max_pdf_mb'] = 1
        with patch.object(oa, 'http_get', return_value=self.response(b'%PDF-' + b'x'*1048576 + b'%%EOF')):
            self.assertFalse(oa.try_download_pdf('https://example.org/paper', self.cfg)['ok'])

    def test_big_meta_link_pdf(self):
        self.cfg['max_pdf_mb'] = 1
        html = b'<meta name="citation_pdf_url" content="https://example.org/full.pdf">'
        with patch.object(oa, 'http_get', side_effect=[self.response(html), self.response(b'%PDF-' + b'x'*1048576 + b'%%EOF')]):
            self.assertFalse(oa.try_download_pdf('https://example.org/paper', self.cfg)['ok'])

    def test_meta_link_small_pdf(self):
        html = b'<meta name="citation_pdf_url" content="https://example.org/full.pdf?a=1&amp;b=2">'
        with patch.object(oa, 'http_get', side_effect=[self.response(html), self.response(pdf_fixture())]) as get:
            self.assertTrue(oa.try_download_pdf('https://example.org/paper', self.cfg)['ok'])
            self.assertEqual(get.call_args.args[0], 'https://example.org/full.pdf?a=1&b=2')

    def test_ignore_reference_pdf_link(self):
        self.assertEqual(oa.extract_pdf_url_from_html('<a href="cited-paper.pdf">reference</a>', 'https://e.org'), [])

    def test_stale_manifest(self):
        path, manifest, _ = self.seed(); path.unlink()
        self.assertIsNone(oa.find_existing(manifest, DOI))

    def test_changed_manifest_file(self):
        path, manifest, _ = self.seed(); path.write_bytes(pdf_fixture('Some changed title'))
        self.assertIsNone(oa.find_existing(manifest, DOI))

    def test_cache_without_network(self):
        path, manifest, _ = self.seed()
        with patch.object(oa, 'resolve_oa', side_effect=AssertionError('network forbidden')):
            self.assertEqual(oa.fetch_one(DOI, self.cfg, self.root)['status'], 'exists')

    def test_download_verified(self):
        resolved = dict(kind='doi', id=DOI, metadata=dict(doi=DOI,title=TITLE), locations=[dict(pdf='https://example.org/x.pdf',source='test')])
        with patch.object(oa,'resolve_oa',return_value=resolved), patch.object(oa,'http_get',return_value=self.response(pdf_fixture())):
            result = oa.fetch_one(DOI,self.cfg,self.root)
        self.assertEqual(result['status'],'downloaded'); self.assertTrue(Path(result['path']).is_absolute())

    def test_download_wrong_identity_pending(self):
        resolved = dict(kind='doi', id=DOI, metadata=dict(doi=DOI,title=TITLE), locations=[dict(pdf='https://example.org/x.pdf')])
        with patch.object(oa,'resolve_oa',return_value=resolved), patch.object(oa,'http_get',return_value=self.response(pdf_fixture('Unrelated full paper'))):
            result = oa.fetch_one(DOI,self.cfg,self.root)
        self.assertEqual(result['status'],'saved_pending_verification')

    def test_openalex_closed_location(self):
        locs = oa.openalex_locations({'locations':[{'is_oa':False,'pdf_url':'https://closed.org/file.pdf'}, {'is_oa':True,'pdf_url':'https://oa.org/file.pdf'}]})
        self.assertEqual([a['pdf'] for a in locs], ['https://oa.org/file.pdf'])

    def test_case_sensitive_url_dedup(self):
        self.assertEqual(len(oa.dedupe_locations([{'pdf':'https://e.org/A.pdf'}, {'pdf':'https://e.org/a.pdf'}])),2)

    def test_network_error_is_not_no_oa(self):
        def unavailable(*a):
            self.cfg['_source_errors'].append({'reason':'network_error'})
            return {'kind':'doi','id':DOI,'locations':[]}
        with patch.object(oa,'resolve_oa',side_effect=unavailable):
            self.assertEqual(oa.fetch_one(DOI,self.cfg,self.root)['status'],'source_unavailable')

    def test_pmc_not_oa(self):
        with patch.object(oa,'http_json',return_value={'resultList':{'result':[{'pmcid':'PMC123','isOpenAccess':'N'}]}}), patch.object(oa,'polite_sleep'):
            self.assertEqual(oa.europepmc_locations(None,None,'PMC123',self.cfg),[])

    def test_batch_partial(self):
        p=self.root/'list.txt';p.write_text(DOI+'\ninvalid\n')
        args=argparse.Namespace(file=str(p),out=str(self.root),force=False)
        out=io.StringIO()
        with patch.object(oa,'fetch_one',side_effect=[{'status':'downloaded'},ValueError()]), patch.object(oa,'polite_sleep'),contextlib.redirect_stdout(out):
            code=oa.cmd_batch(args,self.cfg)
        data=json.loads(out.getvalue());self.assertEqual(code,1);self.assertEqual(data['failed'],1)

    def test_import_and_organize(self):
        path, manifest, record=self.seed()
        # One file can have different absolute spellings (e.g. Windows short paths).
        alias=self.root/'nested'; alias.mkdir()
        record['path']=str(alias/'..'/path.name)
        manifest.write_text(json.dumps(record)+'\n',encoding='utf-8')
        args=argparse.Namespace(file=str(path),doi=DOI,title=TITLE,out=str(self.root),source_url='https://e.org/paper?token=secret')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(oa.cmd_import_file(args,self.cfg),0)
            self.assertEqual(oa.cmd_organize(argparse.Namespace(dir=str(self.root)),self.cfg),0)
        self.assertNotIn('secret',manifest.read_text())
        with (self.root/'index.csv').open(encoding='utf-8-sig',newline='') as handle:
            rows=list(csv.DictReader(handle))
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['title'],TITLE)
        self.assertEqual(rows[0]['doi'],DOI)
        self.assertEqual(list(self.root.glob('*.pdf')),[path])

    def test_rewrite_idempotent(self):
        url=vpn.to_webvpn('https://example.org/file?x=1#page=3')
        self.assertEqual(vpn.to_webvpn(url),url); self.assertTrue(url.endswith('#page=3'))

    def test_rewrite_unverified_port(self):
        with self.assertRaises(ValueError): vpn.to_webvpn('https://example.org:8443/file')

    def test_rewrite_base_and_credentials(self):
        for base in ['http://vpn.org','https://user:pass@vpn.org','https://vpn.org/path']:
            with self.subTest(base=base), self.assertRaises(ValueError): vpn.to_webvpn('https://example.org',base)
        with self.assertRaises(ValueError): vpn.to_webvpn('https://user:pass@example.org')

if __name__=='__main__': unittest.main(verbosity=2)
