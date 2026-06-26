#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IPTV 流验证模块

提供多级验证：
  Phase 1: HTTP HEAD + Content-Type 校验
  Phase 2: 内容探测（读取前4KB，验证是否为真实的流内容）
  错误分类、IPv6检测、过期token检测、质量评分
"""

import re
import json
import os
import socket
from urllib.parse import urlparse

import requests


# ── 加载配置 ──────────────────────────────────────────────

def _load_json_config(filename):
    """加载同目录下的JSON配置文件"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


_domain_rules = _load_json_config('domain_rules.json') or {}
_quality_tiers = _load_json_config('quality_tiers.json') or {}


# ── 正则模式 ──────────────────────────────────────────────

_RE_IPV6_URL = re.compile(r'https?://\[([0-9a-fA-F:]+)\]')
_RE_RAW_IP = re.compile(r'https?://(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})')
_RE_STATIC_EXT = re.compile(r'\.(mp4|mp3|avi|mkv|flv|wmv|mov|webm|jpg|png|gif)(\?|$)', re.I)
_RE_EXTINF_NAME = re.compile(r',(?P<name>[^,]*)$')
_RE_GROUP_TITLE = re.compile(r'group-title="([^"]*)"')

# 过期/临时 token 特征
_TOKEN_PATTERNS = [
    re.compile(p, re.I) for p in [
        r'GuardEncType=',
        r'accountinfo=',
        r'txSecret=',
        r'txTime=',
        r'auth_key=',
        r'auth_token=',
        r'sign=',
        r'expires=',
        r'expire=',
        r'token=',
    ]
]

# 有效的流 Content-Type 前缀
_VALID_CONTENT_TYPES = [
    'video/',
    'audio/',
    'application/vnd.apple.mpegurl',
    'application/x-mpegURL',
    'application/x-mpegurl',
    'application/mpegurl',
    'application/octet-stream',
    'text/plain',
    'binary/octet-stream',
    'model/vnd.mpegurl',
]

# Content-Type 黑名单（明确不是流）
_INVALID_CONTENT_TYPES = [
    'text/html',
    'text/css',
    'text/javascript',
    'application/json',
    'application/xml',
    'image/',
    'font/',
]

# MPEG-TS 同步字节
_TS_SYNC_BYTE = 0x47


# ── 工具函数 ──────────────────────────────────────────────

def is_ipv6_url(url):
    """检测URL是否使用IPv6地址"""
    return bool(_RE_IPV6_URL.search(url))


def is_raw_ip(url):
    """检测URL是否使用裸IP地址（非域名）"""
    return bool(_RE_RAW_IP.search(url))


def is_rtmp_url(url):
    """检测是否为RTMP/RTSP协议"""
    return url.lower().startswith(('rtmp://', 'rtsp://'))


def extract_domain(url):
    """从URL提取域名或IP"""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ''
        if _RE_IPV6_URL.search(url):
            m = _RE_IPV6_URL.search(url)
            return f'[{m.group(1)}]'
        return host
    except Exception:
        return ''


def extract_tld(domain):
    """提取域名的TLD后缀"""
    if not domain or _RE_RAW_IP.match(f'http://{domain}'):
        return ''
    parts = domain.rsplit('.', 1)
    return f'.{parts[1]}' if len(parts) == 2 else ''


def has_token_params(url):
    """检测URL是否包含可能过期的认证token参数"""
    return any(p.search(url) for p in _TOKEN_PATTERNS)


def has_static_extension(url):
    """检测URL是否指向静态文件而非直播流"""
    return bool(_RE_STATIC_EXT.search(url.replace('/stream/', '/')))


def parse_extinf_name(extinf_line):
    """从#EXTINF行解析频道名称"""
    m = _RE_EXTINF_NAME.search(extinf_line)
    if m:
        return m.group('name').strip()
    return None


def parse_group_title(extinf_line):
    """从#EXTINF行解析group-title"""
    m = _RE_GROUP_TITLE.search(extinf_line)
    if m:
        return m.group(1)
    return ''


def normalize_channel_name(name):
    """规范化频道名称用于去重比较"""
    if not name:
        return ''
    # 移除多余空格、标点差异
    name = name.strip()
    name = re.sub(r'\s+', '', name)
    name = name.replace('-', '').replace('_', '').replace('·', '')
    name = name.replace('（', '(').replace('）', ')')
    name = name.replace('HD', '').replace('hd', '')
    name = name.replace('高清', '').replace('标清', '')
    name = name.replace('4K', '').replace('4k', '')
    return name.lower()


# ── StreamValidator ───────────────────────────────────────

class StreamValidator:
    """IPTV流多级验证器"""

    def __init__(self, timeout=5, content_probe_timeout=8, max_workers=10):
        self.timeout = timeout
        self.connect_timeout = 3
        self.content_probe_timeout = content_probe_timeout
        self.max_workers = max_workers
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/125.0.0.0 Safari/537.36'
            ),
            'Accept': '*/*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
        })

        # 从配置加载
        self.blocklist_exact = set(
            _domain_rules.get('blocklist', {}).get('domains_exact', [])
        )
        self.blocklist_suffix = set(
            _domain_rules.get('blocklist', {}).get('domains_suffix', [])
        )
        self.trustlist = set(
            _domain_rules.get('trustlist', {}).get('domains', [])
        )
        self.raw_ip_handling = _domain_rules.get('raw_ip_handling', 'flag_only')

        # 质量评分配置
        scoring = _quality_tiers.get('scoring', {})
        self.base_score = scoring.get('base_score', 5)
        self.bonuses = scoring.get('bonuses', {})
        self.penalties = scoring.get('penalties', {})
        self.score_range = scoring.get('clamp_range', [0, 10])

    # ── URL 分析 ──────────────────────────────────────

    def analyze_url(self, url):
        """分析URL，返回其属性"""
        result = {
            'url': url,
            'is_ipv6': is_ipv6_url(url),
            'is_raw_ip': is_raw_ip(url),
            'is_rtmp': is_rtmp_url(url),
            'has_static_ext': has_static_extension(url),
            'has_token': has_token_params(url),
            'domain': extract_domain(url),
            'tld': '',
            'scheme': '',
        }
        try:
            parsed = urlparse(url)
            result['scheme'] = parsed.scheme
        except Exception:
            pass

        result['tld'] = extract_tld(result['domain'])
        return result

    # ── 域名规则检查 ──────────────────────────────────

    def check_domain_rules(self, url_info):
        """根据域名规则检查URL，返回规则命中结果"""
        domain = url_info.get('domain', '')

        if not domain:
            return {
                'is_blocked': False, 'is_suspicious': False,
                'is_trusted': False, 'is_raw_ip': url_info.get('is_raw_ip', False),
                'reason': 'no_domain'
            }

        # 检查精确拦截
        if domain in self.blocklist_exact:
            return {
                'is_blocked': True, 'is_suspicious': True,
                'is_trusted': False, 'is_raw_ip': False,
                'reason': f'blocklist_exact:{domain}'
            }

        # 检查TLD后缀
        tld = url_info.get('tld', '')
        if tld in self.blocklist_suffix:
            return {
                'is_blocked': False, 'is_suspicious': True,
                'is_trusted': False, 'is_raw_ip': False,
                'reason': f'blocklist_suffix:{tld}'
            }

        # 检查裸IP
        if url_info.get('is_raw_ip'):
            if self.raw_ip_handling == 'block':
                return {
                    'is_blocked': True, 'is_suspicious': True,
                    'is_trusted': False, 'is_raw_ip': True,
                    'reason': 'raw_ip_blocked'
                }
            return {
                'is_blocked': False, 'is_suspicious': True,
                'is_trusted': False, 'is_raw_ip': True,
                'reason': 'raw_ip_flagged'
            }

        # 检查信任列表
        if domain in self.trustlist:
            return {
                'is_blocked': False, 'is_suspicious': False,
                'is_trusted': True, 'is_raw_ip': False,
                'reason': f'trustlist:{domain}'
            }

        return {
            'is_blocked': False, 'is_suspicious': False,
            'is_trusted': False, 'is_raw_ip': False,
            'reason': 'neutral'
        }

    # ── Phase 1: HTTP HEAD ────────────────────────────

    def validate_head(self, url, retries=2):
        """Phase 1: HTTP HEAD请求 + Content-Type检查"""
        result = {
            'reachable': False,
            'status_code': 0,
            'content_type': '',
            'content_length': -1,
            'final_url': url,
            'redirect_count': 0,
            'response_time_ms': 0,
            'error_class': 'unknown',
            'error_message': '',
        }

        last_error = None
        for attempt in range(retries + 1):
            try:
                resp = self.session.head(
                    url,
                    timeout=(self.connect_timeout, self.timeout),
                    allow_redirects=True,
                    stream=True,
                )
                elapsed_ms = resp.elapsed.total_seconds() * 1000

                result['reachable'] = True
                result['status_code'] = resp.status_code
                result['content_type'] = resp.headers.get('Content-Type', '')
                result['content_length'] = int(resp.headers.get('Content-Length', -1))
                result['response_time_ms'] = round(elapsed_ms, 1)

                # 跟踪重定向
                if resp.history:
                    result['redirect_count'] = len(resp.history)
                    result['final_url'] = resp.url

                # 分类状态码
                if resp.status_code in (200, 302, 301, 307, 308):
                    result['error_class'] = 'ok'
                elif resp.status_code == 403:
                    result['error_class'] = 'forbidden'
                    result['error_message'] = f'HTTP {resp.status_code}'
                elif resp.status_code == 404:
                    result['error_class'] = 'not_found'
                    result['error_message'] = f'HTTP {resp.status_code}'
                elif resp.status_code in (401, 402):
                    result['error_class'] = 'auth_required'
                    result['error_message'] = f'HTTP {resp.status_code}'
                elif 400 <= resp.status_code < 500:
                    result['reachable'] = False
                    result['error_class'] = 'client_error_4xx'
                    result['error_message'] = f'HTTP {resp.status_code}'
                elif 500 <= resp.status_code < 600:
                    result['reachable'] = False
                    result['error_class'] = 'server_error_5xx'
                    result['error_message'] = f'HTTP {resp.status_code}'
                else:
                    result['reachable'] = False
                    result['error_class'] = 'unexpected_status'
                    result['error_message'] = f'HTTP {resp.status_code}'

                return result

            except requests.exceptions.Timeout:
                last_error = 'timeout'
            except requests.exceptions.ConnectionError as e:
                err_str = str(e).lower()
                if 'refused' in err_str or 'connection aborted' in err_str:
                    last_error = 'connection_refused'
                elif 'name or service not known' in err_str or 'getaddrinfo' in err_str:
                    last_error = 'dns_failure'
                elif 'reset' in err_str:
                    last_error = 'connection_reset'
                else:
                    last_error = 'connection_error'
            except requests.exceptions.SSLError:
                last_error = 'ssl_error'
            except requests.exceptions.TooManyRedirects:
                last_error = 'too_many_redirects'
            except Exception as e:
                last_error = 'unknown'
                result['error_message'] = str(e)[:200]

            # 重试前等待
            if attempt < retries:
                import time
                time.sleep(1.0 * (attempt + 1))

        result['error_class'] = last_error or 'unknown'
        result['error_message'] = result['error_message'] or last_error or ''
        return result

    # ── Phase 2: 内容探测 ─────────────────────────────

    def probe_content(self, url, content_type_hint=''):
        """Phase 2: 读取流内容前4KB，验证是否为真实流"""
        result = {
            'is_stream': False,
            'content_type_actual': '',
            'hls_valid': False,
            'mpegts_valid': False,
            'preview_size': 0,
            'preview_hex': '',
            'error': '',
        }

        # 如果内容类型是HTML，直接判定为非流
        if content_type_hint:
            ct_lower = content_type_hint.lower()
            is_invalid = any(
                ct_lower.startswith(prefix) for prefix in _INVALID_CONTENT_TYPES
            )
            if is_invalid:
                result['error'] = f'invalid_content_type:{content_type_hint}'
                return result

        try:
            resp = self.session.get(
                url,
                timeout=(self.connect_timeout, self.content_probe_timeout),
                stream=True,
                allow_redirects=True,
            )

            if resp.status_code not in (200, 206):
                result['error'] = f'HTTP_{resp.status_code}'
                return result

            result['content_type_actual'] = resp.headers.get('Content-Type', '')

            # 再次检查实际Content-Type
            actual_ct = result['content_type_actual'].lower()
            if any(actual_ct.startswith(prefix) for prefix in _INVALID_CONTENT_TYPES):
                result['error'] = f'actual_invalid_ct:{result["content_type_actual"]}'
                return result

            # 读取最多4KB
            chunk = b''
            for data in resp.iter_content(chunk_size=4096):
                chunk = data
                break

            result['preview_size'] = len(chunk)

            if len(chunk) == 0:
                result['error'] = 'empty_response'
                return result

            # 检查内容
            content_text = None
            try:
                content_text = chunk.decode('utf-8', errors='replace')
            except Exception:
                pass

            # 检查是否为 HLS (m3u8)
            if content_text and content_text.lstrip().startswith('#EXTM3U'):
                result['hls_valid'] = True
                result['is_stream'] = True

                # 进一步验证m3u8内容
                lines = content_text.splitlines()
                has_stream_ref = any(
                    line.endswith('.ts') or line.endswith('.m3u8') or
                    'bandwidth' in line.lower() or '#EXT-X-STREAM-INF' in line or
                    '#EXTINF' in line
                    for line in lines
                )
                if not has_stream_ref:
                    # 可能是简单的播放列表重定向
                    has_http = any(
                        line.startswith('http') for line in lines
                    )
                    if has_http:
                        result['is_stream'] = True
                    else:
                        result['is_stream'] = False
                        result['error'] = 'm3u8_no_stream_ref'
                return result

            # 检查是否为 MPEG-TS (0x47 同步字节)
            if len(chunk) >= 188 and chunk[0] == _TS_SYNC_BYTE:
                # 验证多个TS包（每188字节一个同步字节）
                sync_count = sum(
                    1 for i in range(0, min(len(chunk), 188 * 4), 188)
                    if chunk[i] == _TS_SYNC_BYTE
                )
                if sync_count >= 2:
                    result['mpegts_valid'] = True
                    result['is_stream'] = True
                    return result

            # 其他二进制内容 - 如果Content-Type看起来像视频则接受
            resp_ct = result['content_type_actual'].lower()
            looks_like_stream = any(
                resp_ct.startswith(prefix) for prefix in _VALID_CONTENT_TYPES
            )
            if looks_like_stream and len(chunk) > 100:
                result['is_stream'] = True
                return result

            # 看起来像HTML或文本但又不是m3u8
            if content_text and (
                '<html' in content_text.lower() or '<!doctype' in content_text.lower()
            ):
                result['error'] = 'html_response'
                return result

            # 默认：有够大的二进制响应就接受
            if len(chunk) > 200:
                result['is_stream'] = True
            else:
                result['error'] = f'too_small:{len(chunk)}bytes'

            return result

        except requests.exceptions.Timeout:
            result['error'] = 'probe_timeout'
        except requests.exceptions.ConnectionError:
            result['error'] = 'probe_connection_error'
        except Exception as e:
            result['error'] = f'probe_exception:{str(e)[:100]}'

        return result

    # ── 完整验证流程 ─────────────────────────────────

    def validate_channel(self, channel):
        """对单个频道执行完整验证流程"""
        url = channel.get('url', '')
        name = channel.get('name', 'Unknown')

        # 1. URL分析
        url_info = self.analyze_url(url)

        # 2. 域名规则
        domain_result = self.check_domain_rules(url_info)

        # 3. 非HTTP协议特殊处理
        if url_info['is_rtmp']:
            return {
                **self._build_base_result(channel, url_info, domain_result),
                'valid': None,  # 不可验证
                'phase1': {'reachable': None, 'error_class': 'rtmp_unverifiable'},
                'phase2': {'is_stream': None, 'error': 'rtmp_protocol'},
                'verdict': 'unverifiable',
                'score': max(0, self.base_score + self.penalties.get('non_hls_protocol', -1)),
            }

        # 4. Phase 1: HEAD检查
        head_result = self.validate_head(url)

        # 5. Phase 2: 内容探测（仅Phase 1通过时）
        probe_result = {'is_stream': False, 'error': 'skipped'}
        if head_result['reachable']:
            probe_result = self.probe_content(url, head_result['content_type'])

        # 6. 综合判断
        is_valid = (
            head_result['reachable'] and
            probe_result['is_stream'] and
            not domain_result['is_blocked']
        )

        # 7. 评分
        score = self._calculate_score(
            url_info, domain_result, head_result, probe_result, channel
        )

        verdict = self._assign_verdict(score, is_valid, domain_result)

        return {
            **self._build_base_result(channel, url_info, domain_result),
            'valid': is_valid,
            'phase1': head_result,
            'phase2': probe_result,
            'verdict': verdict,
            'score': score,
        }

    def _build_base_result(self, channel, url_info, domain_result):
        return {
            'channel': channel.get('name', 'Unknown'),
            'url': channel.get('url', ''),
            'url_info': url_info,
            'domain_rules': domain_result,
        }

    def _calculate_score(self, url_info, domain_result, head_result, probe_result, channel):
        """计算质量评分"""
        score = self.base_score

        # 加分项
        if domain_result['is_trusted']:
            score += self.bonuses.get('domain_in_trustlist', 3)

        if probe_result.get('hls_valid'):
            score += self.bonuses.get('content_verified_hls', 2)
        elif probe_result.get('is_stream'):
            score += self.bonuses.get('content_verified_generic', 1)

        # CDN特征域名
        domain = url_info.get('domain', '')
        if any(kw in domain.lower() for kw in ['cdn', 'live', 'stream', 'hls', 'play']):
            score += self.bonuses.get('cdn_like_hostname', 1)

        if url_info.get('scheme') == 'https':
            score += self.bonuses.get('https_enabled', 1)

        # 减分项
        if domain_result.get('reason', '').startswith('blocklist_exact'):
            score += self.penalties.get('domain_in_blocklist_exact', -5)

        if domain_result.get('reason', '').startswith('blocklist_suffix'):
            score += self.penalties.get('domain_in_blocklist_suffix', -2)

        if domain_result.get('is_raw_ip'):
            score += self.penalties.get('raw_ip_address', -2)

        if url_info.get('has_token'):
            score += self.penalties.get('has_expired_token_params', -3)

        if url_info.get('has_static_ext'):
            score += self.penalties.get('static_file_extension', -5)

        # 非电视内容检测
        name = channel.get('name', '')
        group = parse_group_title(channel.get('raw_extinf', ''))
        if self._is_non_tv(name, group):
            score += self.penalties.get('non_tv_content', -3)

        if url_info.get('is_rtmp'):
            score += self.penalties.get('non_hls_protocol', -1)

        # 限制范围
        low, high = self.score_range
        return max(low, min(high, score))

    def _assign_verdict(self, score, is_valid, domain_result):
        """根据分数分配等级"""
        if domain_result.get('is_blocked'):
            return 'F'
        if not is_valid:
            return 'F'
        if score <= 0:
            return 'F'
        if score >= 8:
            return 'A'
        if score >= 5:
            return 'B'
        return 'C'

    def _is_non_tv(self, name, group_title=''):
        """检测是否为非电视内容"""
        # 静态文件扩展名
        if has_static_extension(name):
            return True

        # 景区/慢直播
        webcam_keywords = [
            '直播中国', '风景', '景观', '风景区', '慢直播', '监控',
            '熊猫', 'ipanda', '摄像头', '古城', '雪山', '瀑布',
            '日出', '云海', '观鸟', '动物园', '水族馆', '天文',
        ]
        if any(kw in name for kw in webcam_keywords):
            return True
        if group_title == '直播中国':
            return True

        # 更新时间占位符
        if name.startswith('20') and ('更新' in name or ':' in name):
            return True

        return False


# ── 便捷函数 ──────────────────────────────────────────────

def create_validator():
    """创建默认配置的验证器实例"""
    return StreamValidator()


def quick_analyze(url):
    """快速分析单个URL"""
    v = StreamValidator()
    return v.analyze_url(url)
