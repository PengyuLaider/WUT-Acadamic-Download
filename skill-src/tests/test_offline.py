"""Offline regression tests: all external HTTP and resolver operations are mocked."""
import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import paper_oa as oa
import pdf_check
import webvpn_url as vpn

TITLE = 'A reliable campus paper download'
DOI = '10.1234/example(2025)'
DOI2 = '10.1234/second-example'
DOI3 = '10.1234/third-example'
CHINESE_DOI = '10.16383/j.aas.c220001'

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
        data=json.loads(out.getvalue());self.assertEqual(code,3);self.assertEqual(data['failed'],1)

    def test_import_and_organize(self):
        path, manifest, record=self.seed()
        args=argparse.Namespace(file=str(path),doi=DOI,title=TITLE,out=str(self.root),source_url='https://e.org/paper?token=secret')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(oa.cmd_import_file(args,self.cfg),0)
            self.assertEqual(oa.cmd_organize(argparse.Namespace(dir=str(self.root)),self.cfg),0)
        self.assertNotIn('secret',manifest.read_text()); self.assertEqual((self.root/'index.csv').read_text(encoding='utf-8-sig').count(TITLE),1)

    def test_rewrite_idempotent(self):
        url=vpn.to_webvpn('https://example.org/file?x=1#page=3')
        self.assertEqual(vpn.to_webvpn(url),url); self.assertTrue(url.endswith('#page=3'))

    def test_rewrite_unverified_port(self):
        with self.assertRaises(ValueError): vpn.to_webvpn('https://example.org:8443/file')

    def test_rewrite_base_and_credentials(self):
        for base in ['http://vpn.org','https://user:pass@vpn.org','https://vpn.org/path']:
            with self.subTest(base=base), self.assertRaises(ValueError): vpn.to_webvpn('https://example.org',base)
        with self.assertRaises(ValueError): vpn.to_webvpn('https://user:pass@example.org')

    # ------------------------------------------------------------------
    # 统一退出码（0=成功 / 1=参数或配置错误 / 2=需要用户或无合法 OA / 3=部分完成或待核验）
    # ------------------------------------------------------------------
    def test_exit_code_table(self):
        self.assertEqual((oa.EXIT_OK, oa.EXIT_USAGE, oa.EXIT_UNAVAILABLE, oa.EXIT_PARTIAL), (0, 1, 2, 3))
        for status in ('downloaded', 'exists', 'ok'):
            with self.subTest(status=status): self.assertEqual(oa.exit_code_for(status), 0)
        for status in ('no_oa', 'source_unavailable'):
            with self.subTest(status=status): self.assertEqual(oa.exit_code_for(status), 2)
        for status in ('saved_pending_verification', 'download_failed', 'error', 'partial'):
            with self.subTest(status=status): self.assertEqual(oa.exit_code_for(status), 3)
        for status in ('invalid_identifier', 'invalid_argument', 'config_error', 'invalid_pdf'):
            with self.subTest(status=status): self.assertEqual(oa.exit_code_for(status), 1)
        self.assertEqual(oa.exit_code_for('unexpected-status'), 3)
        self.assertEqual(oa.exit_code_for(''), 3)

    def test_main_rejects_bad_arguments_with_exit1(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(oa.main(['--definitely-not-a-flag']), 1)
            self.assertEqual(oa.main([]), 1)

    def test_cmd_fetch_and_oa_exit_codes(self):
        with patch.object(oa, 'fetch_one', return_value={'status': 'download_failed'}):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(oa.cmd_fetch(argparse.Namespace(
                    identifier=DOI, out=str(self.root), force=False), self.cfg), 3)
        with patch.object(oa, 'fetch_one', return_value={'status': 'no_oa'}):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(oa.cmd_fetch(argparse.Namespace(
                    identifier=DOI, out=str(self.root), force=False), self.cfg), 2)
        with patch.object(oa, 'fetch_one', return_value={'status': 'downloaded'}):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(oa.cmd_fetch(argparse.Namespace(
                    identifier=DOI, out=str(self.root), force=False), self.cfg), 0)
        empty = {'kind': 'doi', 'id': DOI, 'metadata': {}, 'locations': []}
        with patch.object(oa, 'resolve_oa', return_value=empty):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(oa.cmd_oa(argparse.Namespace(identifier=DOI), self.cfg), 2)
        unknown = {'kind': 'unknown', 'id': 'x', 'metadata': {}, 'locations': []}
        with patch.object(oa, 'resolve_oa', return_value=unknown):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(oa.cmd_oa(argparse.Namespace(identifier='x'), self.cfg), 1)

    # ------------------------------------------------------------------
    # 中文 DOI（chndoi.org 多重解析 -> doi.org 回退 -> 落地页提取）
    # ------------------------------------------------------------------
    def test_chinese_doi_prefixes(self):
        for doi in (CHINESE_DOI, '10.19721/j.cjme.2024.01.001',
                    '10.15918/j.jbit1004-0579.2024.01',
                    '10.3969/j.issn.1000-1239.2024.01',
                    'https://doi.org/10.16383/x'):
            with self.subTest(doi=doi): self.assertTrue(oa.is_chinese_doi(doi))
        for doi in ('', '10.1234/example', '10.163830/x'):
            with self.subTest(doi=doi): self.assertFalse(oa.is_chinese_doi(doi))
        self.assertTrue(oa.is_chinese_doi('10.9999/article', {'chinese_doi_prefixes': ['10.9999']}))
        self.assertFalse(oa.is_chinese_doi(CHINESE_DOI, {'chinese_doi_prefixes': ['10.9999']}))

    def test_resolve_chinese_doi_chndoi_landing(self):
        landing = 'https://www.aas.net.cn/article/doi/' + CHINESE_DOI
        html = ('<html><head><meta charset="utf-8"></head><body>'
                '<a href="/static/logo.png">PDF</a>'
                '<a href="/article/doi/%s/pdf">PDF下载</a>'
                '<a href="/article/download?id=42">全文下载</a>'
                '</body></html>' % CHINESE_DOI).encode('utf-8')
        with patch.object(oa, 'http_get', return_value=self.response(html, url=landing)) as get:
            result = oa.resolve_chinese_doi(CHINESE_DOI, self.cfg)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['landing_url'], landing)
        pdf_urls = [loc['pdf'] for loc in result['locations'] if loc.get('pdf')]
        self.assertIn(landing + '/pdf', pdf_urls)
        self.assertIn(landing, [loc['url'] for loc in result['locations']])
        self.assertFalse(any('logo.png' in (loc.get('pdf') or loc.get('url') or '')
                             for loc in result['locations']))
        self.assertEqual({loc['source'] for loc in result['locations']}, {'chndoi'})
        self.assertIn('chndoi.org/Resolution/Handler', get.call_args.args[0])
        self.assertIn(oa.parse.quote(CHINESE_DOI, safe=''), get.call_args.args[0])

    def test_resolve_chinese_doi_falls_back_to_doi_org(self):
        landing = 'https://www.cjme.com.cn/article/doi/10.19721/j.cjme.2024.01.001'
        html = ('<html><head><meta name="citation_pdf_url" '
                'content="https://www.cjme.com.cn/article/2024/pdf/x.pdf"></head></html>').encode()
        with patch.object(oa, 'http_get', side_effect=[RuntimeError('chndoi down'),
                                                     self.response(html, url=landing)]) as get:
            result = oa.resolve_chinese_doi('10.19721/j.cjme.2024.01.001', self.cfg)
        self.assertEqual(result['landing_url'], landing)
        self.assertEqual(result['locations'][0]['source'], 'doi-resolver')
        self.assertEqual([loc['pdf'] for loc in result['locations'] if loc.get('pdf')],
                         ['https://www.cjme.com.cn/article/2024/pdf/x.pdf'])
        self.assertEqual(get.call_count, 2)
        self.assertTrue(get.call_args_list[1].args[0].startswith('https://doi.org/'))
        self.assertEqual([error.get('reason') for error in result['source_errors']],
                         ['network_error'])

    def test_chinese_doi_failure_is_source_unavailable_not_no_oa(self):
        def down(*args, **kwargs):
            raise RuntimeError('offline')
        with patch.object(oa, 'http_get', side_effect=down):
            resolved = oa.resolve_chinese_doi(CHINESE_DOI, self.cfg)
        self.assertEqual(resolved['status'], 'source_unavailable')
        self.assertEqual(resolved['locations'], [])
        self.assertTrue(any(error.get('reason') in ('network_error', 'source_unavailable')
                            for error in resolved['source_errors']))
        self.assertTrue(resolved['hints'])

    def test_resolve_oa_chinese_skips_western_sources(self):
        locs = [dict(pdf='https://www.aas.net.cn/x.pdf', url='https://www.aas.net.cn/x.pdf',
                     source='chndoi', version='published', is_best=True)]
        def forbidden(*args, **kwargs):
            raise AssertionError('western source used for chinese DOI')
        with patch.object(oa, 'resolve_chinese_doi',
                          return_value={'status': 'ok', 'locations': locs,
                                        'source_errors': [], 'hints': ['中文 DOI 提示']}), \
             patch.object(oa, 'unpaywall_locations', side_effect=forbidden), \
             patch.object(oa, 'openalex_work', side_effect=forbidden), \
             patch.object(oa, 's2_by_doi', side_effect=forbidden), \
             patch.object(oa, 'crossref_meta', side_effect=forbidden), \
             patch.object(oa, 'arxiv_locations', side_effect=forbidden):
            resolved = oa.resolve_oa(CHINESE_DOI, self.cfg)
        self.assertEqual(resolved['locations'][0]['pdf'], 'https://www.aas.net.cn/x.pdf')
        self.assertEqual(resolved['hints'], ['中文 DOI 提示'])
        self.assertEqual(resolved['metadata']['doi'], CHINESE_DOI)

    def test_fetch_one_chinese_failure_reports_hint(self):
        def chinese_resolve(identifier, cfg):
            cfg['_source_errors'].append({'source': 'chndoi', 'reason': 'source_unavailable'})
            return {'kind': 'doi', 'id': identifier, 'metadata': {'doi': identifier},
                    'locations': [], 'hints': ['中文 DOI 提示']}
        with patch.object(oa, 'resolve_oa', side_effect=chinese_resolve):
            result = oa.fetch_one(CHINESE_DOI, self.cfg, self.root)
        self.assertEqual(result['status'], 'source_unavailable')
        self.assertIn('中文 DOI 提示', result['hints'])

    def test_extract_fulltext_links_filters_static_assets(self):
        page = 'https://journal.example.org/article/1'
        html = ('<a href="/img/logo.png">PDF</a>'
                '<a href="/static/app.js">下载</a>'
                '<a href="/files/paper.pdf">PDF</a>'
                '<a href="/download?file=2">全文下载</a>'
                '<a href="https://doi.org/10.16383/x">doi</a>'
                '<a href="#top">PDF</a>')
        links = oa.extract_fulltext_links(html, page)
        self.assertIn('https://journal.example.org/files/paper.pdf', links)
        self.assertIn('https://journal.example.org/download?file=2', links)
        self.assertFalse(any(asset in link for link in links
                             for asset in ('logo.png', 'app.js', 'doi.org')))
        self.assertEqual(links[0], 'https://journal.example.org/files/paper.pdf')

    def test_meta_refresh_redirect(self):
        html = '<meta http-equiv="refresh" content="0; url=https://j.example.org/abs/1">'
        self.assertEqual(oa.extract_meta_refresh_url(html, 'https://www.chndoi.org/x'),
                         'https://j.example.org/abs/1')
        self.assertIsNone(oa.extract_meta_refresh_url('<meta charset="utf-8">', 'https://e.org'))

    # ------------------------------------------------------------------
    # batch：--workers 并发 / --state 断点 / --resume 续跑
    # ------------------------------------------------------------------
    def batch_args(self, items, workers=3, resume=False, state=None, force=False):
        path = self.root / 'batch-list.txt'
        path.write_text('\n'.join(items) + '\n', encoding='utf-8')
        return argparse.Namespace(file=str(path), out=str(self.root), force=force,
                                  workers=workers, resume=resume, state=state)

    def run_batch(self, args, fetch):
        out = io.StringIO()
        with patch.object(oa, 'fetch_one', side_effect=fetch), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = oa.cmd_batch(args, self.cfg)
        return code, json.loads(out.getvalue())

    def test_batch_workers_write_state_and_resume(self):
        def fake(identifier, cfg, out_dir, force=False):
            p=self.root/(hashlib.sha256(identifier.encode()).hexdigest()+'.pdf');p.write_bytes(pdf_fixture(doi=identifier))
            return {'status':'downloaded','path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'doi':identifier,'title':TITLE}
        code, data = self.run_batch(self.batch_args([DOI, DOI2, DOI3]), fake)
        self.assertEqual(code, 0)
        self.assertEqual((data['total'], data['downloaded'], data['skipped']), (3, 3, 0))
        self.assertEqual(data['status'], 'ok')
        state = self.root / 'batch-state.jsonl'
        lines = [json.loads(line) for line in state.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertEqual(set(line), {'identifier', 'status', 'path', 'error', 'timestamp','doi','title','sha256'})
            self.assertEqual(line['status'], 'downloaded')
        # --resume：已完成条目全部跳过，不再调用 fetch_one
        code2, data2 = self.run_batch(
            self.batch_args([DOI, DOI2, DOI3], workers=2, resume=True),
            AssertionError('resume must skip completed items'))
        self.assertEqual(code2, 0)
        self.assertEqual((data2['skipped'], data2['downloaded'], data2['incomplete']), (3, 0, 0))

    def test_batch_resume_retries_missing_or_unverified(self):
        state = self.root / 'custom-state.jsonl'
        state.write_text('\n'.join(
            json.dumps({'identifier': ident, 'status': status, 'path': None,
                        'error': None, 'timestamp': 't'})
            for ident, status in [(DOI, 'downloaded'), (DOI2, 'saved_pending_verification')]
        ) + '\n', encoding='utf-8')
        code, data = self.run_batch(
            self.batch_args([DOI, DOI2], workers=2, resume=True, state=str(state)),
            lambda *a,**k:{'status':'download_failed'})
        self.assertEqual(code, 3)
        self.assertEqual((data['skipped'], data['total']), (0, 2))
        self.assertEqual({item['status'] for item in data['results']}, {'download_failed'})

    def test_batch_invalid_workers(self):
        code, data = self.run_batch(self.batch_args([DOI], workers=0),
                                    AssertionError('must not run'))
        self.assertEqual(code, 1)
        self.assertEqual(data['status'], 'invalid_argument')

    def test_batch_missing_file(self):
        args = argparse.Namespace(file=str(self.root / 'missing.txt'), out=str(self.root),
                                  force=False, workers=3, resume=False, state=None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = oa.cmd_batch(args, self.cfg)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())['status'], 'error')

    def test_batch_records_errors_in_state(self):
        code, data = self.run_batch(self.batch_args([DOI]), ValueError('boom'))
        self.assertEqual(code, 3)
        self.assertEqual((data['failed'], data['incomplete']), (1, 1))
        lines = [json.loads(line) for line in
                 (self.root / 'batch-state.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(lines[-1]['status'], 'error')
        self.assertEqual(lines[-1]['error'], 'ValueError')

    def test_batch_workers_one_keeps_input_order(self):
        def fake(identifier, cfg, out_dir, force=False):
            return {'status': 'downloaded', 'identifier': identifier}
        code, data = self.run_batch(self.batch_args([DOI, DOI2], workers=1), fake)
        self.assertEqual(code, 0)
        self.assertEqual([item['identifier'] for item in data['results']], [DOI, DOI2])

    # ------------------------------------------------------------------
    # import-file：单行 JSON / --name / version & source 字段 / organize 索引
    # ------------------------------------------------------------------
    def import_pdf(self, body=None, sub='incoming'):
        folder = self.root / sub
        folder.mkdir(exist_ok=True)
        path = folder / 'paper.pdf'
        path.write_bytes(body or pdf_fixture())
        return path

    def import_args(self, file, **overrides):
        values = dict(file=str(file), doi=DOI, title=TITLE, out=str(self.root / 'out'),
                      source_url='', name='', authors='', year=None, venue='',
                      version_type='unknown', source_type='local', pretty=False)
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_import_name_renames_even_when_source_is_in_out_dir(self):
        source = self.import_pdf(sub='same-out-dir')
        out = source.parent
        args = self.import_args(source, out=str(out), name='自定义归档名')
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = oa.cmd_import_file(args, self.cfg)
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(Path(payload['path']).name, '自定义归档名.pdf')
        self.assertTrue((out / '自定义归档名.pdf').is_file())

    def test_import_single_line_json_with_metadata(self):
        source = self.import_pdf()
        out = self.root / 'out'
        args = self.import_args(source, name='自动化学报_2024', authors='张三;李四', year=2024,
                                venue='自动化学报', version_type='published',
                                source_type='publisher',
                                source_url='https://e.org/article?token=secret')
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = oa.cmd_import_file(args, self.cfg)
        self.assertEqual(code, 0)
        raw = buffer.getvalue()
        self.assertEqual(raw.count('\n'), 1)
        payload = json.loads(raw)
        archived = out / '自动化学报_2024.pdf'
        self.assertTrue(archived.is_file())
        self.assertEqual(Path(payload['path']), archived)
        self.assertEqual((payload['version'], payload['source_type'], payload['source']),
                         ('published', 'publisher', 'publisher'))
        self.assertEqual(payload['authors'], ['张三', '李四'])
        self.assertEqual(payload['year'], 2024)
        self.assertNotIn('secret', (out / 'manifest.jsonl').read_text(encoding='utf-8'))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(oa.cmd_organize(argparse.Namespace(dir=str(out)), self.cfg), 0)
        index = (out / 'index.csv').read_text(encoding='utf-8-sig')
        self.assertIn('version', index.splitlines()[0])
        self.assertIn('source_type', index.splitlines()[0])
        self.assertIn('published', index)
        self.assertIn('publisher', index)

    def test_import_default_name_uses_doi_slug(self):
        source = self.import_pdf(sub='incoming-doi')
        out = self.root / 'out-doi'
        args = self.import_args(source, title='', doi='10.9999/xyz', out=str(out),
                                source_url='https://e.org/x')
        with contextlib.redirect_stdout(io.StringIO()):
            code = oa.cmd_import_file(args, self.cfg)
        self.assertEqual(code, 3)  # 无题名证据 -> 待核验
        names = sorted(path.name for path in out.glob('*.pdf'))
        self.assertEqual(names, ['10.9999_xyz.pdf'])
        self.assertFalse(any(name.startswith('nd_anon') for name in names))

    def test_import_default_name_without_metadata_uses_paper(self):
        source = self.import_pdf(sub='incoming-paper')
        out = self.root / 'out-paper'
        args = self.import_args(source, title='', doi='', out=str(out))
        with contextlib.redirect_stdout(io.StringIO()):
            code = oa.cmd_import_file(args, self.cfg)
        self.assertEqual(code, 3)
        self.assertEqual([path.name for path in out.glob('*.pdf')], ['paper.pdf'])

    def test_import_pretty_output(self):
        source = self.import_pdf(sub='incoming-pretty')
        out = self.root / 'out-pretty'
        args = self.import_args(source, out=str(out), pretty=True)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = oa.cmd_import_file(args, self.cfg)
        self.assertEqual(code, 0)
        text = buffer.getvalue()
        self.assertGreater(text.count('\n'), 1)
        self.assertEqual(json.loads(text)['status'], 'downloaded')

    def test_import_pending_verification_exit3(self):
        source = self.import_pdf(pdf_fixture('Another unrelated paper'), sub='incoming-pending')
        out = self.root / 'out-pending'
        args = self.import_args(source, out=str(out))
        with contextlib.redirect_stdout(io.StringIO()):
            code = oa.cmd_import_file(args, self.cfg)
        self.assertEqual(code, 3)
        self.assertEqual([path.name for path in out.glob('*.pdf')], [TITLE + '.pdf'])

    def test_import_invalid_pdf_exit1(self):
        source = self.root / 'not-a.pdf'
        source.write_bytes(b'<html>not a pdf</html>')
        out = self.root / 'out-invalid'
        args = self.import_args(source, out=str(out))
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = oa.cmd_import_file(args, self.cfg)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(buffer.getvalue())['status'], 'invalid_pdf')
        self.assertFalse(out.exists() and any(out.glob('*.pdf')))

    # ------------------------------------------------------------------
    # 线程安全：state/manifest 追加锁、请求限速锁
    # ------------------------------------------------------------------
    def test_append_jsonl_is_thread_safe(self):
        target = self.root / 'state.jsonl'
        def worker(number):
            for index in range(5):
                oa.append_jsonl(target, {'n': number, 'i': index})
        threads = [threading.Thread(target=worker, args=(number,)) for number in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        lines = [json.loads(line) for line in target.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(lines), 40)

    def test_pace_request_serializes_same_host_with_lock(self):
        clock = {'now': 0.0}
        slept = []
        guard = threading.Lock()
        def fake_monotonic():
            with guard:
                return clock['now']
        def fake_sleep(seconds):
            with guard:
                slept.append(seconds)
                clock['now'] += seconds
        start = threading.Event()
        def worker():
            start.wait()
            oa.pace_request(self.cfg, 'example.org')
        with patch.object(oa.time, 'monotonic', side_effect=fake_monotonic), \
             patch.object(oa.time, 'sleep', side_effect=fake_sleep):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            start.set()
            for thread in threads:
                thread.join()
        # 同一 host 的第二个请求必须等待满一个 request_interval_seconds
        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], 1.0)

    def test_doi_slug_and_split_authors(self):
        self.assertEqual(oa.doi_slug('10.1234/example(2025)'), '10.1234_example_2025')
        self.assertEqual(oa.doi_slug(''), '')
        self.assertEqual(oa.split_authors('张三;李四, Wang Wu、赵六'),
                         ['张三', '李四', 'Wang Wu', '赵六'])

    def test_build_pdf_name_avoids_nd_anon(self):
        self.assertEqual(oa.build_pdf_name({'title': 'Some title', 'year': 2024,
                                            'authors': ['Wang Wu']}, '10.1/x'),
                         '2024_Wu_Some title.pdf')
        self.assertEqual(oa.build_pdf_name({'title': 'Some title'}, '10.1/x'),
                         'Some title.pdf')
        self.assertEqual(oa.build_pdf_name({}, '10.16383/j.aas.c220001'),
                         '10.16383_j.aas.c220001.pdf')
        self.assertNotIn('nd_anon', oa.build_pdf_name({'title': 'T'}, '10.1/x'))

if __name__=='__main__': unittest.main(verbosity=2)
