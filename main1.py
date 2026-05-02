import pandas as pd
import time
import re
import os
import copy
import unicodedata
import multiprocessing
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager

# ─────────────────────────────────────────────────────────────────────────────
# MODULE-LEVEL WORKER STATE
# Each worker process gets its own Chrome driver via the initializer.
# Globals are safe here because multiprocessing gives each worker its own
# memory space — there is no shared state between workers.
# ─────────────────────────────────────────────────────────────────────────────
_worker_driver = None


def _worker_init(headless: bool):
    """Called once per worker process at pool startup."""
    global _worker_driver
    options = Options()
    if headless:
        options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    options.add_argument("window-size=1920,1080")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    service = Service(ChromeDriverManager().install())
    _worker_driver = webdriver.Chrome(service=service, options=options)
    _worker_driver.execute_cdp_cmd('Network.setUserAgentOverride', {
        "userAgent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    })
    _worker_driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )


# ─────────────────────────────────────────────────────────────────────────────
# STATELESS HELPER FUNCTIONS  (picklable — safe to use in worker processes)
# ─────────────────────────────────────────────────────────────────────────────

def _make_safe_folder_name(name: str) -> str:
    """
    Converts a story title into a safe folder name.
    - Strips characters invalid on Windows/macOS/Linux
    - Collapses whitespace
    - Limits to 80 chars to avoid path-length issues
    """
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name)
    safe = re.sub(r'\s+', ' ', safe).strip()
    safe = safe.strip('. ')          # Windows dislikes leading/trailing dots
    return safe[:80] if safe else "Unknown_Story"


def _scroll_to_load_full_chapter(driver):
    """Scrolls until no new paragraphs appear."""
    print(f"      → Scrolling to load full chapter...")
    last_count = 0
    stall_attempts = 0
    max_stalls = 3

    while stall_attempts < max_stalls:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2.5)
        current_count = driver.execute_script(
            "return document.querySelectorAll('p[data-p-id]').length;"
        )
        if current_count > last_count:
            print(f"      → {current_count} paragraphs loaded so far...")
            last_count = current_count
            stall_attempts = 0
        else:
            stall_attempts += 1

    print(f"      → Full chapter loaded: {last_count} paragraphs total.")


def _extract_chapter_text(soup):
    """Extracts story text. Must be called BEFORE any decompose() operations."""
    text_content = []
    paragraphs = soup.find_all('p', attrs={'data-p-id': True})

    if paragraphs:
        for p in paragraphs:
            p_copy = copy.copy(p)
            for ui in p_copy.find_all(
                ['div', 'button'],
                class_=re.compile(r'component-wrapper|comment-marker')
            ):
                ui.decompose()
            para_text = p_copy.get_text(strip=True)
            if para_text:
                text_content.append(para_text)

    if not text_content:
        pre_tag = soup.find('pre', class_=re.compile(r'chapter-text|story-text'))
        if pre_tag:
            pre_copy = copy.copy(pre_tag)
            for ui in pre_copy.find_all(
                ['div', 'button'],
                class_=re.compile(r'component-wrapper|comment-marker')
            ):
                ui.decompose()
            for p in pre_copy.find_all('p'):
                para_text = p.get_text(strip=True)
                if para_text:
                    text_content.append(para_text)

    return '\n\n'.join(text_content) if text_content else ""


def _scrape_stats_from_soup(soup):
    """Extracts Reads/Votes/Comments from a chapter page soup."""
    stats = {"Reads": "N/A", "Votes": "N/A", "Comments": "N/A"}

    # Noise removal
    for noise in soup.find_all(
        ['div', 'section', 'aside'],
        class_=re.compile(r'recommend|sidebar|next-up|story-list|similar|related|footer|you-may-also')
    ):
        noise.decompose()
    for div in soup.find_all('div'):
        text = div.get_text(strip=True)
        if any(p in text.lower() for p in ['you may also like', 'recommended', 'similar stories', 'more stories']):
            if len(text) < 500:
                div.decompose()

    # Method 1: story-stats div
    stats_div = soup.find('div', class_='story-stats')
    if stats_div:
        reads_span = stats_div.find('span', class_='reads')
        if reads_span:
            title_attr = (reads_span.get('title') or
                          reads_span.get('data-original-title') or
                          reads_span.get('data-toggle'))
            if title_attr:
                m = re.search(r'([\d,]+)\s+Read', title_attr, re.IGNORECASE)
                if m:
                    stats['Reads'] = m.group(1).replace(',', '')
        votes_span = stats_div.find('span', class_='votes')
        if votes_span:
            m = re.search(r'([\d,]+)', votes_span.get_text(strip=True))
            if m:
                stats['Votes'] = m.group(1).replace(',', '')
        comments_span = stats_div.find('span', class_='comments')
        if comments_span:
            m = re.search(r'([\d,]+)', comments_span.get_text(strip=True))
            if m:
                stats['Comments'] = m.group(1).replace(',', '')

    # Method 2: sr-only spans
    if stats['Reads'] == "N/A" or stats['Votes'] == "N/A":
        main_area = soup.find('div', id='story-reading') or soup.find('article') or soup
        for span in main_area.find_all('span', class_='sr-only'):
            text = span.get_text(strip=True)
            rm = re.search(r'Reads?\s+([\d,]+)',    text, re.IGNORECASE)
            vm = re.search(r'Votes?\s+([\d,]+)',    text, re.IGNORECASE)
            cm = re.search(r'Comments?\s+([\d,]+)', text, re.IGNORECASE)
            if rm and stats['Reads']    == "N/A": stats['Reads']    = rm.group(1).replace(',', '')
            if vm and stats['Votes']    == "N/A": stats['Votes']    = vm.group(1).replace(',', '')
            if cm and stats['Comments'] == "N/A": stats['Comments'] = cm.group(1).replace(',', '')
            if all(v != "N/A" for v in stats.values()):
                break

    # Method 3: data-toggle tooltip
    if stats['Reads'] == "N/A":
        header = soup.find('header') or soup.find('div', class_=re.compile(r'story-info|chapter-info'))
        area = header if header else soup
        for el in area.find_all(attrs={"data-toggle": "tooltip"}):
            title = el.get('title', '') or el.get('data-original-title', '')
            if 'read' in title.lower():
                m = re.search(r'([\d,]+)\s+Read', title, re.IGNORECASE)
                if m:
                    stats['Reads'] = m.group(1).replace(',', '')
                    break

    # Method 4: aria-label
    if stats['Reads'] == "N/A" or stats['Votes'] == "N/A":
        num_regex = r'([\d,]+(?:\.\d+)?\s*[KMB]?)'
        main_content = soup.find('div', id='story-reading') or soup.find('article') or soup
        for el in main_content.find_all(attrs={"aria-label": True}):
            label = el['aria-label'].lower()
            val_match = re.search(num_regex, label, re.IGNORECASE)
            if val_match:
                num = val_match.group(1).replace(' ', '').replace(',', '')
                if 'read'      in label and stats['Reads']    == "N/A": stats['Reads']    = num
                elif 'vote'    in label and stats['Votes']    == "N/A": stats['Votes']    = num
                elif 'comment' in label and stats['Comments'] == "N/A": stats['Comments'] = num
            if all(v != "N/A" for v in stats.values()):
                break

    # Method 5: visible meta text
    if stats['Reads'] == "N/A" or stats['Votes'] == "N/A":
        main_content = soup.find('div', id='story-reading') or soup.find('article')
        if main_content:
            meta_parts = (
                main_content.select('[class*="meta"] span') +
                main_content.select('[class*="stats"] span')
            )
            nums = []
            for m in meta_parts:
                txt = m.get_text(separator=" ", strip=True)
                for f in re.findall(r'[\d,]+(?:\.\d+)?\s*[KMB]?', txt, re.IGNORECASE):
                    if re.search(r'\d', f):
                        clean = f.replace(' ', '').replace(',', '')
                        if clean not in nums:
                            nums.append(clean)
            if stats['Reads']    == "N/A" and len(nums) >= 1: stats['Reads']    = nums[0]
            if stats['Votes']    == "N/A" and len(nums) >= 2: stats['Votes']    = nums[1]
            if stats['Comments'] == "N/A" and len(nums) >= 3: stats['Comments'] = nums[2]

    return stats


# ─────────────────────────────────────────────────────────────────────────────
# WORKER TASK  (top-level — required for multiprocessing pickling)
# ─────────────────────────────────────────────────────────────────────────────

def _scrape_chapter_task(args):
    """
    Executed in a worker process for a single chapter.

    Args:
        args: tuple of (chapter_dict, extract_text: bool)

    Returns:
        chapter_dict updated with stats and optionally Chapter_Text.
    """
    global _worker_driver
    chapter, extract_text = args
    url = chapter['URL']

    try:
        pid = os.getpid()
        print(f"    [PID {pid}] Scanning: {url[:55]}...")
        _worker_driver.get(url)
        time.sleep(3)

        if extract_text:
            _scroll_to_load_full_chapter(_worker_driver)
            _worker_driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(1)

        soup = BeautifulSoup(_worker_driver.page_source, 'html.parser')

        if extract_text:
            chapter['Chapter_Text'] = _extract_chapter_text(soup)

        stats = _scrape_stats_from_soup(soup)
        chapter.update(stats)

        print(f"    [PID {pid}] ✓ {chapter['Title'][:30]:30} "
              f"R:{stats['Reads']:>6} V:{stats['Votes']:>4} C:{stats['Comments']:>4}")

    except Exception as e:
        print(f"    [ERROR] {url[:55]} → {e}")
        chapter.update({"Reads": "Error", "Votes": "Error", "Comments": "Error"})
        if extract_text:
            chapter['Chapter_Text'] = ""

    return chapter


# ─────────────────────────────────────────────────────────────────────────────
# MAIN SCRAPER CLASS
# ─────────────────────────────────────────────────────────────────────────────

class WattpadScraperV5Parallel:
    """
    Multi-URL Parallel Wattpad scraper — v5.1

    For each story URL:
      outputs/<Story Title>/
      ├── story_metadata.csv
      ├── chapters_metadata.csv
      ├── FULL_STORY.txt               ← all chapters combined in order
      └── chapters/
          ├── 01 - Chapter Title.txt
          ├── 02 - Chapter Title.txt
          └── ...
    """

    def __init__(self, headless=True, scrape_chapter_stats=True,
                 extract_chapter_text=False, num_workers=3):
        self.headless             = headless
        self.should_scrape_stats  = scrape_chapter_stats
        self.should_extract_text  = extract_chapter_text
        self.num_workers          = num_workers
        self._main_driver         = self._make_driver(headless=False)

    # ── driver factory ───────────────────────────────────────────────────────

    def _make_driver(self, headless=False):
        options = Options()
        if headless:
            options.add_argument('--headless=new')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)
        options.add_argument("window-size=1920,1080")
        options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.execute_cdp_cmd('Network.setUserAgentOverride', {
            "userAgent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        })
        driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        return driver

    # ── text helpers ─────────────────────────────────────────────────────────

    def normalize_text(self, text):
        if not text:
            return ""
        normalized = unicodedata.normalize('NFKD', text)
        ascii_text = normalized.encode('ascii', 'ignore').decode('ascii').strip()
        return re.sub(r'\s+', ' ', ascii_text)

    # ── page loading ─────────────────────────────────────────────────────────

    def _load_page_content(self):
        print("    ...Loading full page content...")
        total_height = self._main_driver.execute_script("return document.body.scrollHeight")
        for i in range(1, 6):
            self._main_driver.execute_script(f"window.scrollTo(0, {total_height * (i/5)});")
            time.sleep(0.8)
        try:
            toc = self._main_driver.find_element(By.CLASS_NAME, "story-parts")
            self._main_driver.execute_script(
                "arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", toc
            )
            time.sleep(2)
        except Exception:
            self._main_driver.execute_script(f"window.scrollTo(0, {total_height * 0.4});")
            time.sleep(2)
        try:
            buttons = self._main_driver.find_elements(
                By.XPATH, "//button[contains(text(), 'Show more') or contains(@class, 'more')]"
            )
            for btn in buttons:
                if btn.is_displayed():
                    self._main_driver.execute_script("arguments[0].click();", btn)
                    time.sleep(1)
        except Exception:
            pass

    # ── parsers ──────────────────────────────────────────────────────────────

    def parse_metadata_bs4(self, html_content, url):
        soup = BeautifulSoup(html_content, 'html.parser')

        title = "Unknown"
        title_tag = (soup.find('h1') or
                     soup.select_one('.story-info h1') or
                     soup.select_one('[class*="title"]'))
        if title_tag:
            title = title_tag.get_text(strip=True)
        else:
            match = re.search(r'/story/\d+-([^?]+)', url)
            if match:
                from urllib.parse import unquote
                title = unquote(match.group(1)).replace('-', ' ')

        author = "Unknown"
        for a in soup.find_all('a', href=re.compile(r'^/user/')):
            class_str = str(a.get('class', '')).lower()
            if 'avatar' not in class_str and 'profile' not in class_str:
                text = a.get_text(strip=True)
                if text and len(text) > 1 and text.lower() not in ['wattpad', 'home']:
                    author = text
                    break
        if author == "Unknown":
            page_title = soup.find('title')
            if page_title:
                parts = page_title.get_text(strip=True).split('-')
                if len(parts) >= 3:
                    possible = parts[-2].strip()
                    if possible.lower() != "wattpad":
                        author = possible

        desc = ""
        desc_tag = (soup.find('pre') or
                    soup.select_one('.description') or
                    soup.select_one('.story-description'))
        if desc_tag:
            desc = desc_tag.get_text("\n", strip=True)

        reads = votes = parts = "0"
        for span in soup.find_all('span', class_='sr-only'):
            text = span.get_text(strip=True)
            rm = re.search(r'Reads?\s+([\d,]+)', text, re.IGNORECASE)
            vm = re.search(r'Votes?\s+([\d,]+)', text, re.IGNORECASE)
            pm = re.search(r'Parts?\s+(\d+)',    text, re.IGNORECASE)
            if rm: reads = rm.group(1).replace(',', '')
            if vm: votes = vm.group(1).replace(',', '')
            if pm: parts = pm.group(1)

        if reads == "0" or votes == "0":
            num_regex = r'([\d,]+(?:\.\d+)?\s*[KMB]?)'
            for el in soup.find_all(attrs={"aria-label": True}):
                label = el['aria-label'].lower()
                val_match = re.search(num_regex, label, re.IGNORECASE)
                if val_match:
                    num = val_match.group(1).replace(' ', '').replace(',', '')
                    if 'read' in label and reads == "0": reads = num
                    elif 'vote' in label and votes == "0": votes = num
                    elif 'part' in label and parts == "0": parts = num

        tags = []
        for t in soup.find_all('a', class_=re.compile(r'pill__')):
            tag_text = t.get_text(strip=True)
            if tag_text:
                tags.append(tag_text)
        if not tags:
            for t in soup.select('a[href*="/stories/"]'):
                class_str = str(t.get('class', ''))
                if 'tag' in class_str or 'pill' in class_str:
                    tags.append(t.get_text(strip=True))

        return {
            "Story_ID":    url.split('/')[-1].split('-')[0] if '/' in url and '-' in url else "Unknown",
            "Title":       self.normalize_text(title).title(),
            "Author":      self.normalize_text(author),
            "Description": self.normalize_text(desc),
            "Total_Reads": reads,
            "Total_Votes": votes,
            "Total_Parts": parts,
            "Tags":        " | ".join(tags[:15]),
            "Story_URL":   url
        }

    def parse_chapters_bs4(self, html_content):
        soup = BeautifulSoup(html_content, 'html.parser')
        chapters = []
        seen_urls = set()

        story_parts = soup.find('ul', attrs={'aria-label': 'story-parts'})
        if story_parts:
            for li in story_parts.find_all('li', recursive=False):
                link = li.find('a', href=True)
                if not link:
                    continue
                href = link['href']
                if '/story/' in href or not re.search(r'/\d+-', href):
                    continue
                title_div = link.find('div', class_='wpYp-')
                title = title_div.get_text(strip=True) if title_div else "Unknown"
                date_div = link.find('div', class_='bSGSB')
                date = date_div.get_text(strip=True) if date_div else "Unknown"
                full_url = href if href.startswith('http') else f"https://www.wattpad.com{href}"
                if full_url not in seen_urls and title:
                    seen_urls.add(full_url)
                    chapters.append({
                        "Order":          len(chapters) + 1,
                        "Title":          self.normalize_text(title),
                        "URL":            full_url,
                        "Published_Date": date
                    })

        if not chapters:
            for link in soup.find_all('a', href=True):
                href = link['href']
                text = link.get_text(strip=True)
                if not re.search(r'/\d+-', href): continue
                if '/story/' in href: continue
                if any(x in href for x in ['/user/', '/list/', '/login', '/search', '/myworks']): continue
                full_url = href if href.startswith('http') else f"https://www.wattpad.com{href}"
                if full_url not in seen_urls and text:
                    date = "Unknown"
                    parent = link.find_parent('li') or link.find_parent('div')
                    if parent:
                        parent_text = parent.get_text(" ", strip=True)
                        date_match = re.search(r'([A-Z][a-z]{2},\s+[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})', parent_text)
                        if not date_match:
                            date_match = re.search(r'([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})', parent_text)
                        if date_match:
                            date = date_match.group(1)
                    seen_urls.add(full_url)
                    chapters.append({
                        "Order":          len(chapters) + 1,
                        "Title":          self.normalize_text(text),
                        "URL":            full_url,
                        "Published_Date": date
                    })

        return chapters

    # ── parallel chapter scraping ─────────────────────────────────────────────

    def _run_parallel_chapter_scrape(self, chapters, story_title):
        print(f"\n{'='*70}")
        print(f"PARALLEL SCRAPING: {len(chapters)} chapters | "
              f"{self.num_workers} workers | '{story_title}'")
        if self.should_extract_text:
            print("  (Text extraction enabled — scroll-to-load per chapter)")
        print(f"{'='*70}")

        tasks = [(ch, self.should_extract_text) for ch in chapters]
        start_time = time.time()

        ctx = multiprocessing.get_context('spawn')
        with ctx.Pool(
            processes=self.num_workers,
            initializer=_worker_init,
            initargs=(self.headless,)
        ) as pool:
            results = pool.map(_scrape_chapter_task, tasks)

        elapsed = time.time() - start_time
        print(f"\n  ✓ Scraped in {elapsed:.1f}s "
              f"({elapsed/max(len(chapters),1):.1f}s avg per chapter)")

        results.sort(key=lambda x: x.get('Order', 0))
        return results

    # ── output saving ─────────────────────────────────────────────────────────

    def _save_story_outputs(self, story_meta, chapters, story_dir):
        """
        Saves all outputs inside story_dir:
          story_metadata.csv
          chapters_metadata.csv
          FULL_STORY.txt
          chapters/
            01 - Title.txt
            02 - Title.txt
            ...
        """
        os.makedirs(story_dir, exist_ok=True)

        # ── CSVs ──────────────────────────────────────────────────────────────
        df_story    = pd.DataFrame([story_meta])
        df_chapters = pd.DataFrame(chapters)

        df_story.to_csv(
            os.path.join(story_dir, "story_metadata.csv"),
            index=False, encoding='utf-8-sig'
        )
        df_chapters.to_csv(
            os.path.join(story_dir, "chapters_metadata.csv"),
            index=False, encoding='utf-8-sig'
        )
        print(f"  ✓ story_metadata.csv")
        print(f"  ✓ chapters_metadata.csv")

        if not self.should_extract_text or 'Chapter_Text' not in df_chapters.columns:
            return  # nothing more to write

        # ── Individual chapter .txt files ─────────────────────────────────────
        chapters_dir = os.path.join(story_dir, "chapters")
        os.makedirs(chapters_dir, exist_ok=True)
        saved_individually = 0

        for idx, row in df_chapters.iterrows():
            text = row.get('Chapter_Text', '')
            if not pd.notna(text) or not text:
                continue
            ch_title   = row.get('Title', f"Chapter {idx+1}")
            safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', ch_title).strip()[:100]
            filename   = f"{idx+1:02d} - {safe_title}.txt"
            filepath   = os.path.join(chapters_dir, filename)
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(f"{ch_title}\n")
                f.write("=" * len(ch_title) + "\n\n")
                f.write(text)
            saved_individually += 1

        print(f"  ✓ {saved_individually} individual chapter files → chapters/")

        # ── FULL_STORY.txt  (pure story text — no metadata) ──────────────────
        chapters_with_text = [
            row for _, row in df_chapters.iterrows()
            if pd.notna(row.get('Chapter_Text', '')) and row.get('Chapter_Text', '')
        ]

        full_story_path = os.path.join(story_dir, "FULL_STORY.txt")
        with open(full_story_path, 'w', encoding='utf-8') as f:
            for row in chapters_with_text:
                f.write(row.get('Chapter_Text', ''))
                f.write('\n\n')

        total_chars = sum(len(row.get('Chapter_Text', '')) for row in chapters_with_text)
        print(f"  ✓ FULL_STORY.txt  ({total_chars:,} chars, "
              f"{len(chapters_with_text)} chapters combined)")

    # ── single story runner ───────────────────────────────────────────────────

    def _run_single_story(self, url):
        """Scrapes one story and saves outputs to outputs/<story title>/"""
        print(f"\n{'='*70}")
        print(f"SCRAPING: {url}")
        print(f"{'='*70}")

        self._main_driver.get(url)
        time.sleep(5)
        self._load_page_content()
        html_source = self._main_driver.page_source

        print("\n  Parsing metadata...")
        story_meta = self.parse_metadata_bs4(html_source, url)

        print("  Parsing chapter list...")
        chapters = self.parse_chapters_bs4(html_source)
        print(f"  Found {len(chapters)} chapters.")

        if chapters:
            story_meta['Total_Parts'] = str(len(chapters))

        # Build output folder from story title
        folder_name = _make_safe_folder_name(story_meta['Title'])
        story_dir   = os.path.join("outputs", folder_name)

        print(f"\n  Title:   {story_meta['Title']}")
        print(f"  Author:  {story_meta['Author']}")
        print(f"  Reads:   {story_meta['Total_Reads']}")
        print(f"  Votes:   {story_meta['Total_Votes']}")
        print(f"  Parts:   {story_meta['Total_Parts']}")
        print(f"  Folder:  {story_dir}")

        return story_meta, chapters, story_dir

    # ── public entry point ────────────────────────────────────────────────────

    def run(self, urls: list):
        """
        Scrapes one or more story URLs sequentially (story pages),
        with parallel chapter scraping within each story.

        Args:
            urls: list of Wattpad story URLs
        """
        urls = [u.strip() for u in urls if u.strip()]
        if not urls:
            print("[ERROR] No URLs provided.")
            return

        print(f"\n{'='*70}")
        print(f"WATTPAD MULTI-STORY SCRAPER  —  {len(urls)} story/stories")
        print(f"{'='*70}")

        all_story_meta = []

        try:
            for story_num, url in enumerate(urls, 1):
                print(f"\n[Story {story_num}/{len(urls)}]")

                story_meta, chapters, story_dir = self._run_single_story(url)

                if self.should_scrape_stats and chapters:
                    # Quit main driver before spawning workers to free memory
                    self._main_driver.quit()
                    self._main_driver = None

                    chapters = self._run_parallel_chapter_scrape(
                        chapters, story_meta['Title']
                    )

                    # Restart main driver for the next story (if any remaining)
                    if story_num < len(urls):
                        print("\n  Restarting main driver for next story...")
                        self._main_driver = self._make_driver(headless=False)

                print(f"\n  Saving outputs to: {story_dir}/")
                self._save_story_outputs(story_meta, chapters, story_dir)
                all_story_meta.append(story_meta)

        except Exception as e:
            print(f"\n[ERROR] {e}")
            import traceback
            traceback.print_exc()

        finally:
            if self._main_driver:
                try:
                    self._main_driver.quit()
                except Exception:
                    pass

        # ── Summary CSV across all stories ────────────────────────────────────
        if all_story_meta:
            os.makedirs("outputs", exist_ok=True)
            summary_path = os.path.join("outputs", "all_stories_summary.csv")
            pd.DataFrame(all_story_meta).to_csv(
                summary_path, index=False, encoding='utf-8-sig'
            )
            print(f"\n{'='*70}")
            print(f"ALL DONE — {len(all_story_meta)} story/stories scraped")
            print(f"Summary saved to: {summary_path}")
            print(f"{'='*70}")
            for meta in all_story_meta:
                folder = _make_safe_folder_name(meta['Title'])
                print(f"  • outputs/{folder}/")


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def _load_urls_from_file(filepath: str) -> list:
    """
    Reads URLs from a plain text file — one URL per line.
    Ignores blank lines and lines starting with # (comments).
    """
    filepath = filepath.strip().strip('"').strip("'")
    if not os.path.exists(filepath):
        print(f"  [ERROR] File not found: {filepath}")
        return []
    urls = []
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                urls.append(line)
    return urls


if __name__ == "__main__":
    multiprocessing.freeze_support()

    print("=" * 70)
    print("WATTPAD SCRAPER V5.1 — Multi-URL + Parallel + Per-Story Folders")
    print("=" * 70)
    print()
    print("Modes:")
    print("  1. Metadata + Chapters only  (no chapter page visits)")
    print("  2. Metadata + Chapters + Stats  (parallel)")
    print("  3. Metadata + Chapters + Stats + Full TEXT  (parallel)")
    print()

    choice = input("Enter choice (1/2/3): ").strip()

    # ── URL input method ──────────────────────────────────────────────────────
    print()
    print("URL source:")
    print("  F. Load from a .txt file  (one URL per line, # for comments)")
    print("  M. Enter URLs manually")
    print()

    url_source = input("Enter choice (F/M): ").strip().upper()

    urls = []

    if url_source == 'F':
        txt_path = input("\nEnter path to .txt file: ").strip().strip('"').strip("'")
        urls = _load_urls_from_file(txt_path)
        if urls:
            print(f"\n  ✓ Loaded {len(urls)} URL(s) from '{txt_path}':")
            for i, u in enumerate(urls, 1):
                print(f"    {i}. {u}")
        else:
            print("  No valid URLs found in file.")

    else:  # Manual entry
        print()
        print("Enter Wattpad story URLs (one per line, blank line when done):")
        print()
        while True:
            line = input("  URL: ").strip().strip('"').strip("'")
            if not line:
                break
            urls.append(line)

    if not urls:
        # Fallback demo URL
        urls = ["https://www.wattpad.com/story/353975883-homecoming"]
        print(f"\nNo URLs provided — using demo: {urls[0]}")

    # ── Worker count ──────────────────────────────────────────────────────────
    workers_input = input("\nNumber of parallel workers per story (Enter for 3): ").strip()
    num_workers   = int(workers_input) if workers_input.isdigit() else 3

    scrape_stats = choice in ('2', '3')
    extract_text = choice == '3'

    print(f"\n{'='*70}")
    print(f"  Stories   : {len(urls)}")
    print(f"  Mode      : {'Stats + Text' if extract_text else 'Stats only' if scrape_stats else 'Metadata only'}")
    print(f"  Workers   : {num_workers} parallel Chrome instances per story")
    print(f"{'='*70}\n")

    scraper = WattpadScraperV5Parallel(
        headless=True,
        scrape_chapter_stats=scrape_stats,
        extract_chapter_text=extract_text,
        num_workers=num_workers
    )
    scraper.run(urls)