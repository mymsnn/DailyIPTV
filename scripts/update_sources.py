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

    # 去重
    unique_channels = {}
    for channel in all_channels:
        url = channel['url']
        if url not in unique_channels:
            unique_channels[url] = channel

    unique_channels_list = list(unique_channels.values())
    self.log(f"去重后频道数量: {len(unique_channels_list)}")

    # 验证频道有效性（抽样验证）
    self.log("开始验证频道有效性（抽样检查）...")
    valid_channels = []
    sample_size = min(100, len(unique_channels_list))

    for i, channel in enumerate(unique_channels_list[:sample_size]):
        if self.is_url_accessible(channel['url']):
            valid_channels.append(channel)
        if (i + 1) % 10 == 0 or (i + 1) == sample_size:
            self.log(f"已验证 {i + 1}/{sample_size} 个频道")

    validity_ratio = len(valid_channels) / sample_size if sample_size > 0 else 0
    estimated_valid_count = int(len(unique_channels_list) * validity_ratio)
    self.log(
        f"有效性抽样比例: {validity_ratio:.2%}, "
        f"估计有效频道: {estimated_valid_count}"
    )

    # 分类
    categorized_channels = {'cctv': [], 'satellite': [], 'local': [], 'international': [], 'other': []}
    for channel in unique_channels_list:
        category = self.categorize_channel(channel['name'])
        categorized_channels[category].append(channel)

    # 创建输出目录
    os.makedirs('outputs', exist_ok=True)
    os.makedirs('logs', exist_ok=True)

    # 保存完整列表
    full_content = self.generate_m3u_content(unique_channels_list)
    with open('outputs/full.m3u', 'w', encoding='utf-8') as f:
        f.write(full_content)

    # 保存分类文件
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

    # 更新README
    self.update_readme(stats)

    self.log(f"=== 更新完成！耗时: {duration:.2f}秒 ===")
    self.log(f"统计信息: {stats}")
