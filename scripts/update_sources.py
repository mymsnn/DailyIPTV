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
        self.repository_owner = os.environ.get('GITHUB_REPOSITORY_OWNER', '你的用户名')
        self.repository_name = os.environ.get('GITHUB_REPOSITORY', 'DailyIPTV').split('/')[-1]
        
    def log(self, message):
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_message = f"[{timestamp}] {message}"
        print(log_message)
        self.log_messages.append(log_message)
    
    def update_readme(self, stats):
        """更新README文件，添加直播源地址"""
        try:
            # 读取现有的README内容
            with open('README.md', 'r', encoding='utf-8') as f:
                readme_content = f.read()
            
            # 生成直播源地址部分
            base_url = f"https://raw.githubusercontent.com/{self.repository_owner}/{self.repository_name}/main/outputs"
            update_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            live_sources_section = f"""
## 📡 直播源地址

以下是最新的直播源地址（最后更新: {update_time}）：

### 🌐 完整列表
- **M3U格式**: [{base_url}/full.m3u]({base_url}/full.m3u)
- 频道总数: {stats['total_channels']} 个
- 估计有效频道: {stats['estimated_valid_channels']} 个
- 有效性比例: {stats['validity_ratio']:.2%}

### 📺 分类列表
- **央视频道**: [{base_url}/cctv.m3u]({base_url}/cctv.m3u) ({stats['categories']['cctv']} 个频道)
- **卫视频道**: [{base_url}/satellite.m3u]({base_url}/satellite.m3u) ({stats['categories']['satellite']} 个频道)
- **地方台**: [{base_url}/local.m3u]({base_url}/local.m3u) ({stats['categories']['local']} 个频道)
- **国际频道**: [{base_url}/international.m3u]({base_url}/international.m3u) ({stats['categories']['international']} 个频道)
- **其他频道**: [{base_url}/other.m3u]({base_url}/other.m3u) ({stats['categories']['other']} 个频道)

### 📊 统计信息
- **更新耗时**: {stats['duration_seconds']} 秒
- **源尝试数**: {stats['sources_attempted']} 个
- **成功源数**: {stats['sources_successful']} 个
- **更新时间**: {stats['update_time']}

### 🚀 快速使用
在支持M3U的播放器（VLC、PotPlayer、Kodi等）中：
1. 打开"打开网络流"
2. 粘贴上述任意链接
3. 享受直播！

---

"""
            
            # 检查是否已经有直播源地址部分，如果有则替换，如果没有则添加
            if '## 📡 直播源地址' in readme_content:
                # 替换现有的直播源部分
                pattern = r'## 📡 直播源地址.*?---'
                updated_readme = re.sub(pattern, live_sources_section.strip(), readme_content, flags=re.DOTALL)
            else:
                # 在文件开头添加直播源部分
                updated_readme = readme_content.replace('# DailyIPTV 📺', f'# DailyIPTV 📺\n{live_sources_section}')
            
            # 写入更新后的README
            with open('README.md', 'w', encoding='utf-8') as f:
                f.write(updated_readme)
            
            self.log("README文件更新成功！")
            return True
            
        except Exception as e:
            self.log(f"更新README文件失败: {e}")
            return False

    # 其他方法保持不变（load_sources, fetch_source, parse_m3u, is_url_accessible, categorize_channel, generate_m3u_content）
    
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
        
        # 更新README文件
        self.update_readme(stats)
        
        self.log(f"=== 更新完成！耗时: {duration:.2f}秒 ===")
        self.log(f"统计信息: {stats}")

if __name__ == "__main__":
    updater = IPTVUpdater()
    updater.run()
