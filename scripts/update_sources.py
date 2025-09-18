#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
import json
import re
from datetime import datetime
import time
import os
from urllib.parse import urlparse

class IPTVUpdater:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })
        self.all_channels = []
        self.log_messages = []
        
    def log(self, message):
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_message = f"[{timestamp}] {message}"
        print(log_message)
        self.log_messages.append(log_message)
        
    def load_sources(self):
        """加载源列表"""
        try:
            with open('scripts/sources_list.json', 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            self.log(f"加载源列表失败: {e}")
            return {"sources": [], "backup_sources": []}
    
    def fetch_source(self, url, timeout=15):
        """获取单个源"""
        try:
            self.log(f"正在获取: {url}")
            response = self.session.get(url, timeout=timeout)
            response.encoding = 'utf-8'
            if response.status_code == 200:
                return response.text
            else:
                self.log(f"获取失败，状态码: {response.status_code}")
                return None
        except Exception as e:
            self.log(f"获取异常: {e}")
            return None
    
    def parse_m3u(self, content, source_url):
        """解析M3U内容"""
        channels = []
        current_channel = {}
        lines = content.splitlines()
        
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
                
            if line.startswith('#EXTINF'):
                # 解析频道信息
                current_channel = {'raw_extinf': line}
                # 提取频道名称
                name_match = re.search(r',(?P<name>.*)$', line)
                if name_match:
                    current_channel['name'] = name_match.group('name').strip()
                else:
                    current_channel['name'] = f"Unknown_{i}"
                    
            elif line.startswith('http://') or line.startswith('https://') or line.startswith('rtsp://') or line.startswith('rtmp://'):
                if current_channel:
                    current_channel['url'] = line
                    current_channel['source'] = source_url
                    channels.append(current_channel)
                    current_channel = {}
        
        self.log(f"从该源解析出 {len(channels)} 个频道")
        return channels
    
    def is_url_accessible(self, url, timeout=8):
        """检查URL是否可访问"""
        try:
            # 只发送HEAD请求检查，节省时间和带宽
            response = requests.head(url, timeout=timeout, allow_redirects=True)
            return response.status_code in [200, 302, 301]
        except:
            try:
                # 如果HEAD失败，尝试GET但只读取头信息
                response = requests.get(url, timeout=timeout, stream=True)
                return response.status_code == 200
            except:
                return False
    
    def categorize_channel(self, channel_name):
        """分类频道"""
        name_lower = channel_name.lower()
        
        # 央视分类
        cctv_keywords = ['cctv', '央视', '中央']
        if any(keyword in name_lower for keyword in cctv_keywords):
            return 'cctv'
        
        # 卫视分类
        satellite_keywords = ['卫视', 'tvb', '凤凰', '星空', '湖南', '浙江', '江苏', '东方', '北京']
        if any(keyword in name_lower for keyword in satellite_keywords):
            return 'satellite'
        
        # 地方台分类
        local_keywords = ['都市', '新闻', '民生', '公共', '教育', '少儿', '体育', '影视', '综艺']
        if any(keyword in name_lower for keyword in local_keywords):
            return 'local'
        
        # 国际频道
        international_keywords = ['bbc', 'cnn', 'nhk', 'fox', 'hbo', 'disney', 'discovery', '国家地理']
        if any(keyword in name_lower for keyword in international_keywords):
            return 'international'
        
        return 'other'
    
    def generate_m3u_content(self, channels):
        """生成M3U内容"""
        header = f"""#EXTM3U
#EXTENC: UTF-8
# Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}
# Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
# Sources: {len(self.sources_config['sources'])} primary, {len(self.sources_config['backup_sources'])} backup
# Total Channels: {len(channels)}
# For personal testing and research purposes only.

"""
        content = header
        for channel in channels:
            content += f"{channel['raw_extinf']}\n{channel['url']}\n"
        
        return content
    
    def run(self):
        """主运行函数"""
        start_time = time.time()
        self.log("=== 开始更新IPTV直播源 ===")
        
        # 加载配置
        self.sources_config = self.load_sources()
        
        # 获取所有源
        all_channels = []
        successful_sources = 0
        
        for source_url in self.sources_config['sources']:
            content = self.fetch_source(source_url)
            if content:
                channels = self.parse_m3u(content, source_url)
                all_channels.extend(channels)
                successful_sources += 1
                time.sleep(1)  # 礼貌延迟
        
        # 如果主要源失败，尝试备用源
        if successful_sources == 0 and self.sources_config['backup_sources']:
            self.log("主要源全部失败，尝试备用源...")
            for backup_url in self.sources_config['backup_sources']:
                content = self.fetch_source(backup_url)
                if content:
                    channels = self.parse_m3u(content, backup_url)
                    all_channels.extend(channels)
                    successful_sources += 1
                    time.sleep(1)
        
        # 去重：基于URL去重
        unique_channels = {}
        for channel in all_channels:
            url = channel['url']
            if url not in unique_channels:
                unique_channels[url] = channel
        
        unique_channels_list = list(unique_channels.values())
        self.log(f"去重后频道数量: {len(unique_channels_list)}")
        
        # 验证频道有效性（抽样验证，避免耗时过长）
        self.log("开始验证频道有效性（抽样检查）...")
        valid_channels = []
        sample_size = min(100, len(unique_channels_list))  # 最多验证100个
        
        for i, channel in enumerate(unique_channels_list[:sample_size]):
            if self.is_url_accessible(channel['url']):
                valid_channels.append(channel)
            if i % 10 == 0:
                self.log(f"已验证 {i+1}/{sample_size} 个频道")
        
        # 假设验证通过的频道比例代表整体
        validity_ratio = len(valid_channels) / sample_size if sample_size > 0 else 0
        estimated_valid_count = int(len(unique_channels_list) * validity_ratio)
        self.log(f"有效性抽样比例: {validity_ratio:.2%}, 估计有效频道: {estimated_valid_count}")
        
        # 分类频道
        categorized_channels = {'cctv': [], 'satellite': [], 'local': [], 'international': [], 'other': []}
        for channel in unique_channels_list:
            category = self.categorize_channel(channel['name'])
            categorized_channels[category].append(channel)
        
        # 生成完整M3U文件
        full_content = self.generate_m3u_content(unique_channels_list)
        with open('outputs/full.m3u', 'w', encoding='utf-8') as f:
            f.write(full_content)
        
        # 生成分类文件
        for category, channels in categorized_channels.items():
            if channels:
                category_content = self.generate_m3u_content(channels)
                with open(f'outputs/{category}.m3u', 'w', encoding='utf-8') as f:
                    f.write(category_content)
        
        # 保存日志
        with open('logs/latest_update.log', 'w', encoding='utf-8') as f:
            f.write('\n'.join(self.log_messages))
        
        # 统计信息
        end_time = time.time()
        duration = end_time - start_time
        stats = {
            'update_time': datetime.now().isoformat(),
            'duration_seconds': round(duration, 2),
            'sources_attempted': len(self.sources_config['sources']) + len(self.sources_config['backup_sources']),
            'sources_successful': successful_sources,
            'total_channels': len(unique_channels_list),
            'estimated_valid_channels': estimated_valid_count,
            'validity_ratio': validity_ratio,
            'categories': {k: len(v) for k, v in categorized_channels.items()}
        }
        
        with open('outputs/stats.json', 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
        
        self.log(f"=== 更新完成！耗时: {duration:.2f}秒 ===")
        self.log(f"统计信息: {stats}")

if __name__ == "__main__":
    updater = IPTVUpdater()
    updater.run()
