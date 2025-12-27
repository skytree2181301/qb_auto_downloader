# -*- coding: utf-8 -*-
import feedparser
import json
import os
from qbittorrent import Client
import google.generativeai as genai
import time
import re
from json.decoder import JSONDecodeError
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs 
import base64

# --- 配置及文件路径 ---
CONFIG_FILE = 'config.json'
SEEN_TORRENTS_FILE = 'seen_torrents.json'

# --- 辅助函数：加载/保存配置和已处理的种子 ---
def load_config():
    """从 config.json 加载配置，并处理JSON解析错误"""
    if not os.path.exists(CONFIG_FILE):
        print(f"错误：配置文件 '{CONFIG_FILE}' 不存在。请根据示例创建并填写。")
        exit()
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except JSONDecodeError as e:
            print(f"错误：配置文件 '{CONFIG_FILE}' JSON 格式无效: {e}")
            exit()
        except Exception as e:
            print(f"错误：加载配置文件 '{CONFIG_FILE}' 时发生未知错误: {e}")
            exit()

def load_seen_torrents():
    """从 seen_torrents.json 加载已处理的种子链接, 并处理空文件或无效JSON"""
    if not os.path.exists(SEEN_TORRENTS_FILE):
        return set() # 文件不存在，返回空集合

    try:
        if os.path.getsize(SEEN_TORRENTS_FILE) == 0:
             return set()
             
        with open(SEEN_TORRENTS_FILE, 'r', encoding='utf-8') as f:
           data = json.load(f)
           return set(data) if isinstance(data, list) else set()
           
    except JSONDecodeError:
        print(f"警告: 无法解析文件 '{SEEN_TORRENTS_FILE}' (文件为空或JSON格式错误)。将返回空集合并继续。")
        return set()
    except Exception as e:
         print(f"警告: 读取文件 '{SEEN_TORRENTS_FILE}' 发生错误: {e}。将返回空集合并继续。")
         return set()

def save_seen_torrents(seen_torrents_set):
    """将已处理的种子链接保存到 seen_torrents.json"""
    with open(SEEN_TORRENTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(seen_torrents_set), f, ensure_ascii=False, indent=4)

# --- 辅助函数：更健壮地提取 Infohash ---
def extract_infohash(link_candidate):
    """
    从磁力链接中提取 Infohash。
    支持标准的 btih (hexadecimal) 和 btih (Base32) 格式。
    统一返回小写十六进制格式。
    """
    if not link_candidate or not link_candidate.startswith('magnet:'):
        return None
    
    parsed_uri = urlparse(link_candidate)
    query_params = parse_qs(parsed_uri.query)
    
    if 'xt' in query_params:
        for xt_param in query_params['xt']:
            if xt_param.startswith('urn:btih:'):
                infohash = xt_param[len('urn:btih:'):]
                
                # 尝试匹配 40 或 64 位十六进制 (SHA-1或SHA-256)
                if re.fullmatch(r'[0-9a-fA-F]{40,64}', infohash):
                    return infohash.lower() # 已经是十六进制，直接返回小写

                # 尝试匹配 Base32 (通常是 32 或 52 字符)
                # Base32 字母集: A-Z, 2-7
                elif re.fullmatch(r'[A-Z2-7]{32,52}', infohash, re.IGNORECASE):
                    try:
                        decoded_bytes = base64.b32decode(infohash.upper(), True) 
                        return decoded_bytes.hex().lower() 
                    except Exception as e:
                        print(f"警告: 无法解码 Base32 infohash '{infohash}': {e}")
                        return None
    return None

# --- AI 决策函数 (Gemini) ---
def decide_with_gemini(title, description, gemini_config):
    """
    使用 Google Gemini 模型决定是否下载、下载路径和标签。
    """
    if not gemini_config.get('api_key'):
        print("错误: Gemini API Key 未配置。无法使用 Gemini 进行决策。")
        return {"action": "skip"}

    genai.configure(api_key=gemini_config['api_key'])
    model = genai.GenerativeModel(gemini_config['model_name'])

    prompt = f"""
你是一个智能的qBittorrent资源筛选助手。你的任务是根据给定的资源标题和描述，判断是否应该下载该资源，并给出推荐的下载路径和标签。
你的目标是专注于筛选动漫音乐资源，特别是高质量、无损格式（如FLAC）的专辑、OST（原声音乐）、VGM（游戏音乐）等。

请输出 JSON 格式的决策结果。JSON 必须包含 'action' 字段，其值可以是 'download' 或 'skip'。
如果 'action' 是 'download'，则必须额外包含 'path' 和 'tags' 字段。
- 'path' 应该是 qBittorrent 中存在的绝对路径，例如 '/downloads/Music/FLAC' 或 '/downloads/Music/OST'。
- 'tags' 是一个字符串列表，例如 ['音乐', '无损', '专辑']。
- 如果不确定，或者判断为非音乐资源，则 'action' 应该是 'skip'。

请严格遵守 JSON 格式输出，不要包含任何额外文字或解释。

示例输出 (下载):
{{
    "action": "download",
    "path": "/downloads/Music/FLAC",
    "tags": ["音乐", "无损", "专辑"]
}}

示例输出 (跳过):
{{
    "action": "skip"
}}

资源标题: {title}
资源描述: {description if description else '无描述'}
"""
    try:
        response = model.generate_content(
            prompt,
            generation_config=genai.GenerationConfig(response_mime_type="application/json")
        )
        
        decision = json.loads(response.text)
        
        if 'action' not in decision:
            print(f"警告: Gemini 输出缺少 'action' 字段: {response.text}")
            return {"action": "skip"}
        
        if decision['action'] == 'download':
            if 'path' not in decision or 'tags' not in decision:
                print(f"警告: Gemini 'download' 决策缺少 'path' 或 'tags' 字段: {response.text}")
                return {"action": "skip"}
            if not isinstance(decision['tags'], list):
                print(f"警告: Gemini 'tags' 字段不是列表: {response.text}")
                decision['tags'] = []
        
        return decision

    except Exception as e:
        print(f"调用 Gemini API 发生错误: {e}")
        print(f"尝试解析的响应文本: {response.text if 'response' in locals() else '无'}")
        return {"action": "skip"}

# --- 主逻辑函数 ---
def main():
    config = load_config()
    seen_torrents = load_seen_torrents()
    
    qb_config = config['qbittorrent']
    gemini_config = config['gemini']
    default_download_path = config.get('default_download_path', '/downloads/Others')
    dry_run = config.get('dry_run', False)

    print(f"脚本以 {'模拟运行模式' if dry_run else '实际运行模式'} 启动。")

    qb = None
    try:
        qb = Client(qb_config['url'])
        qb.login(qb_config['username'], qb_config['password'])
        print(f"成功连接到 qBittorrent ({qb_config['url']}).")
    except Exception as e:
        print(f"连接或登录 qBittorrent 失败: {e}")
        print("请检查 qBittorrent Web UI 是否开启，以及配置文件中的 URL、用户名和密码是否正确。")
        exit()

    # 修正：恢复正确的 RSS Feed 循环结构，确保每个 entry 在循环内处理
    for feed_name, feed_url in config['rss_feeds'].items():
        print(f"\n--- 处理 RSS Feed: {feed_name} ---")
        try: # 捕获整个 Feed 的解析和处理错误
            feed = feedparser.parse(feed_url)
            if feed.bozo:
                print(f"警告: RSS Feed '{feed_name}' 解析错误: {feed.bozo_exception}")

            entries = feed.entries
            print(f"找到 {len(entries)} 个条目。")

            # 内部循环：处理每个 RSS 条目
            for entry in entries:
                original_link = entry.link 
                title = entry.title
                description = entry.get('description', '')
                
                # --- 获取实际下载链接 ---
                actual_download_link = None

                # 1. 优先检查 enclosure 标签
                if hasattr(entry, 'enclosures') and entry.enclosures:
                    for enc in entry.enclosures:
                        if enc.href:
                            if enc.type == 'application/x-bittorrent' or enc.href.startswith('magnet:'):
                                actual_download_link = enc.href
                                # print(f"  通过 enclosure 找到链接: {actual_download_link}") # 移除此行以减少冗余
                                break
                
                # 2. 如果 enclosure 没有提供，则尝试解析原始链接 (网页或直接磁力)
                if not actual_download_link:
                    if original_link and original_link.startswith('magnet:'):
                        actual_download_link = original_link
                        # print(f"  原始链接已经是磁力链接: {actual_download_link}") # 移除此行
                    elif original_link and "share.dmhy.org/topics/view/" in original_link:
                        # print(f"  原始链接是网页，尝试从网页获取实际下载链接: {original_link}") # 移除此行
                        try:
                            headers = {
                                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                            }
                            response = requests.get(original_link, headers=headers, timeout=15)
                            response.raise_for_status()
                            soup = BeautifulSoup(response.text, 'html.parser')

                            magnet_links_on_page = soup.find_all('a', href=re.compile(r'^magnet:'))
                            if magnet_links_on_page:
                                actual_download_link = magnet_links_on_page[0]['href']
                                # print(f"  从网页找到磁力链接: {actual_download_link}") # 移除此行
                            else:
                                torrent_links_on_page = soup.find_all('a', href=re.compile(r'\.torrent$'))
                                if torrent_links_on_page:
                                    relative_path = torrent_links_on_page[0]['href']
                                    actual_download_link = urljoin(original_link, relative_path)
                                    # print(f"  从网页找到 .torrent 文件链接: {actual_download_link}") # 移除此行
                                # else:
                                    # print(f"  未在网页中找到磁力链接或 .torrent 文件链接。") # 移除此行

                        except requests.exceptions.RequestException as req_e:
                            print(f"  访问网页 '{original_link}' 失败: {req_e}")
                        except Exception as parse_e:
                            print(f"  解析网页 '{original_link}' 内容失败: {parse_e}")
                    else:
                        actual_download_link = original_link
                        # print(f"  使用原始链接作为下载链接 (非已知类型): {actual_download_link}") # 移除此行

                # 修正：在确定实际下载链接后，再提取 unique_id
                unique_id = extract_infohash(actual_download_link)
                if not unique_id:
                    unique_id = original_link # 如果无法从实际下载链接提取 infohash，使用原始链接作为唯一标识

                # 检查是否已处理过
                if unique_id in seen_torrents:
                    print(f"  已处理过，跳过: {title}") # 简化输出，不再显示 ID
                    continue

                # 如果未能获取实际下载链接，则跳过此条目（在去重后执行，确保已处理）
                if not actual_download_link:
                    print(f"  未能获取实际下载链接，跳过资源: {title}")
                    seen_torrents.add(unique_id)
                    save_seen_torrents(seen_torrents)
                    continue 
                
                link_to_send_to_qb = actual_download_link

                # --- 优化输出：只显示关键信息 ---
                print(f"\n  评估资源: {title}")

                decision = decide_with_gemini(title, description, gemini_config)

                if decision['action'] == 'download':
                    target_path = decision.get('path', default_download_path)
                    target_tags = decision.get('tags', [])
                    
                    print(f"  决策: 下载! 目标路径: '{target_path}', 标签: {target_tags}")

                    if not dry_run:
                        try:
                            print(f"  发送下载任务到qBittorrent: {title}")
                            qb.download_from_link(
                                link_to_send_to_qb,
                                savepath=target_path,
                                category=','.join(target_tags) if target_tags else None 
                            )
                            
                            added_successfully = False
                            time.sleep(2) 
                            
                            all_torrents = qb.torrents() 
                            
                            torrent_infohash = extract_infohash(link_to_send_to_qb)

                            if torrent_infohash:
                                found_torrent_in_qb = False
                                for torrent in all_torrents:
                                    if torrent['hash'] == torrent_infohash: 
                                        found_torrent_in_qb = True
                                        break
                                
                                if found_torrent_in_qb:
                                    added_successfully = True
                                    print(f"  任务添加成功: {title}")
                                else:
                                    print(f"  警告: Torrent '{title}' 未能在 qBittorrent 列表中找到。") # 简化警告
                                    added_successfully = False # 明确标记为失败，并让外层except捕获
                                    # 不再假设成功，让seen_torrents在except块中处理

                            else:
                                print(f"  警告: 无法精确验证添加，请手动检查qBittorrent。") # 简化警告
                                added_successfully = True # 如果无法验证，仍假定成功，避免无限重试，但减少日志噪音

                            if added_successfully:
                                seen_torrents.add(unique_id) # 只有明确成功或无法验证时才加入 seen_torrents
                                save_seen_torrents(seen_torrents)
                            else:
                                # 如果明确添加失败，则不加入 seen_torrents，以便下次循环可以重新尝试
                                pass # 任务失败时不做去重标记，留给下次重新尝试

                        except Exception as add_e:
                            print(f"  添加下载任务失败 '{title}': {add_e}")
                            # 发生任何添加任务的异常，都标记为已处理，防止无限重试
                            seen_torrents.add(unique_id) 
                            save_seen_torrents(seen_torrents)
                    else:
                        print(f"  (模拟运行) 将下载 '{title}' 到 '{target_path}'，标签: {target_tags}")
                        seen_torrents.add(unique_id)
                        save_seen_torrents(seen_torrents)
            else:
                print(f"  决策: 跳过。")
                seen_torrents.add(unique_id)
                save_seen_torrents(seen_torrents)

            time.sleep(1) # 每一个 entry 处理后的延迟

        except Exception as e: # 这个 try 块的 except，用于捕获整个 RSS 处理过程的错误
            print(f"处理 RSS Feed '{feed_name}' 时发生错误: {e}")
        
        time.sleep(5) # 整个 RSS Feed 处理完后的延迟

    if qb:
        try:
            pass 
        except Exception as e:
            print(f"退出 qBittorrent 登录时发生错误: {e}")

    print("\n脚本执行完毕。")

if __name__ == "__main__":
    main()