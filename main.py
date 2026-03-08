import pandas as pd
import time
import re
import os
import copy
import unicodedata
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

class WattpadScraperV3:
    """
    Improved Wattpad scraper based on actual HTML structure inspection.
    v4.2 Improvements:
    - FIXED: Replaced rel=next URL pagination with scroll-to-load
      (URL pagination caused duplicate text since /page/2 DOM includes page 1 content)
    - FIXED: extract_chapter_text runs BEFORE any decompose() calls
    - FIXED: Uses copy.copy(p) to avoid mutating shared soup object
    - Uses sr-only spans for accurate stat extraction
    - Updated tag selector to use pill__pziVI class
    - Better author detection
    - Improved chapter filtering
    """

    def __init__(self, headless=False, scrape_chapter_stats=True, extract_chapter_text=False):
        self.headless = headless
        self.should_scrape_stats = scrape_chapter_stats
        self.should_extract_text = extract_chapter_text
        self.driver = None
        self._init_driver()

    def _init_driver(self):
        if self.driver:
            try:
                self.driver.quit()
            except:
                pass

        options = Options()
        if self.headless:
            options.add_argument('--headless=new')

        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)
        options.add_argument("window-size=1920,1080")
        options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

        service = Service(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=service, options=options)

        # Stealth patches
        self.driver.execute_cdp_cmd('Network.setUserAgentOverride', {
            "userAgent": 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })
        self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

    def normalize_text(self, text):
        if not text:
            return ""
        normalized = unicodedata.normalize('NFKD', text)
        ascii_text = normalized.encode('ascii', 'ignore').decode('ascii').strip()
        return re.sub(r'\s+', ' ', ascii_text)

    def _parse_volume_string(self, text):
        """Converts strings like '1.5M', '161K', '161,202' to float for comparison."""
        try:
            text = text.upper().replace(',', '').replace(' ', '')
            multiplier = 1
            if 'K' in text:
                multiplier = 1000
                text = text.replace('K', '')
            elif 'M' in text:
                multiplier = 1000000
                text = text.replace('M', '')
            elif 'B' in text:
                multiplier = 1000000000
                text = text.replace('B', '')
            return float(text) * multiplier
        except:
            return 0.0

    def _load_page_content(self):
        """Scrolls and interacts with the story overview page to ensure all chapters load."""
        print("    ...Loading full page content...")

        total_height = self.driver.execute_script("return document.body.scrollHeight")
        steps = 5
        for i in range(1, steps + 1):
            self.driver.execute_script(f"window.scrollTo(0, {total_height * (i/steps)});")
            time.sleep(0.8)

        try:
            toc = self.driver.find_element(By.CLASS_NAME, "story-parts")
            self.driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", toc)
            time.sleep(2)
        except:
            self.driver.execute_script(f"window.scrollTo(0, {total_height * 0.4});")
            time.sleep(2)

        try:
            buttons = self.driver.find_elements(By.XPATH, "//button[contains(text(), 'Show more') or contains(@class, 'more')]")
            for btn in buttons:
                if btn.is_displayed():
                    self.driver.execute_script("arguments[0].click();", btn)
                    time.sleep(1)
        except:
            pass

    def _scroll_to_load_full_chapter(self):
        """
        Scrolls the chapter page slowly until no new paragraphs appear.
        Wattpad lazy-loads paginated content via scroll — this replaces
        the rel=next URL navigation which caused duplicate extraction
        because /page/2 DOM already includes page 1 paragraphs.
        """
        print(f"      → Scrolling to load full chapter...")
        last_count = 0
        stall_attempts = 0
        max_stalls = 3  # Stop after 3 scrolls with no new paragraphs

        while stall_attempts < max_stalls:
            # Scroll to bottom
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2.5)

            # Count current paragraphs in DOM
            current_count = self.driver.execute_script(
                "return document.querySelectorAll('p[data-p-id]').length;"
            )

            if current_count > last_count:
                print(f"      → {current_count} paragraphs loaded so far...")
                last_count = current_count
                stall_attempts = 0  # Reset on progress
            else:
                stall_attempts += 1

        print(f"      → Full chapter loaded: {last_count} paragraphs total.")

    def parse_metadata_bs4(self, html_content, url):
        """Parses story metadata using BeautifulSoup with improved selectors."""
        soup = BeautifulSoup(html_content, 'html.parser')

        # --- Title ---
        title = "Unknown"
        title_tag = soup.find('h1') or soup.select_one('.story-info h1') or soup.select_one('[class*="title"]')
        if title_tag:
            title = title_tag.get_text(strip=True)
        else:
            match = re.search(r'/story/\d+-([^?]+)', url)
            if match:
                from urllib.parse import unquote
                title = unquote(match.group(1)).replace('-', ' ')

        # --- Author ---
        author = "Unknown"
        author_candidates = soup.find_all('a', href=re.compile(r'^/user/'))
        for a in author_candidates:
            class_str = str(a.get('class', '')).lower()
            if 'avatar' not in class_str and 'profile' not in class_str:
                text = a.get_text(strip=True)
                if text and len(text) > 1 and text.lower() not in ['wattpad', 'home']:
                    author = text
                    break

        if author == "Unknown":
            page_title = soup.find('title')
            if page_title:
                pt_text = page_title.get_text(strip=True)
                parts = pt_text.split('-')
                if len(parts) >= 3:
                    possible_author = parts[-2].strip()
                    if possible_author.lower() != "wattpad":
                        author = possible_author

        # --- Description ---
        desc = ""
        desc_tag = soup.find('pre') or soup.select_one('.description') or soup.select_one('.story-description')
        if desc_tag:
            desc = desc_tag.get_text("\n", strip=True)

        # --- Stats (PRIMARY METHOD: sr-only spans) ---
        reads = votes = parts = "0"

        sr_only_spans = soup.find_all('span', class_='sr-only')
        for span in sr_only_spans:
            text = span.get_text(strip=True)
            reads_match = re.search(r'Reads?\s+([\d,]+)', text, re.IGNORECASE)
            votes_match = re.search(r'Votes?\s+([\d,]+)', text, re.IGNORECASE)
            parts_match = re.search(r'Parts?\s+(\d+)', text, re.IGNORECASE)
            if reads_match: reads = reads_match.group(1).replace(',', '')
            if votes_match: votes = votes_match.group(1).replace(',', '')
            if parts_match: parts = parts_match.group(1)

        # Strategy 2: Aria Labels
        if reads == "0" or votes == "0":
            num_regex = r'([\d,]+(?:\.\d+)?\s*[KMB]?)'
            stats_elements = soup.find_all(attrs={"aria-label": True})
            for el in stats_elements:
                label = el['aria-label'].lower()
                val_match = re.search(num_regex, label, re.IGNORECASE)
                if val_match:
                    num = val_match.group(1).replace(' ', '').replace(',', '')
                    if 'read' in label and reads == "0": reads = num
                    elif 'vote' in label and votes == "0": votes = num
                    elif 'part' in label and parts == "0": parts = num

        # Strategy 3: Visible stat displays
        if reads == "0" or votes == "0":
            stat_containers = soup.find_all(['div', 'span'], class_=re.compile(r'stat|meta|info'))
            numbers_found = []
            for container in stat_containers:
                text = container.get_text(separator=" ", strip=True)
                matches = re.findall(r'[\d,]+(?:\.\d+)?\s*[KMB]?', text, re.IGNORECASE)
                for m in matches:
                    if re.search(r'\d', m):
                        clean_m = m.replace(' ', '').replace(',', '')
                        if clean_m not in numbers_found:
                            numbers_found.append(clean_m)
            if reads == "0" and len(numbers_found) >= 1: reads = numbers_found[0]
            if votes == "0" and len(numbers_found) >= 2: votes = numbers_found[1]
            if parts == "0" and len(numbers_found) >= 3: parts = numbers_found[2]

        # --- Tags ---
        tags = []
        tag_elements = soup.find_all('a', class_=re.compile(r'pill__'))
        for t in tag_elements:
            tag_text = t.get_text(strip=True)
            if tag_text:
                tags.append(tag_text)

        if not tags:
            tag_elements = soup.select('a[href*="/stories/"]')
            for t in tag_elements:
                class_str = str(t.get('class', ''))
                if 'tag' in class_str or 'pill' in class_str:
                    tags.append(t.get_text(strip=True))

        return {
            "Story_ID": url.split('/')[-1].split('-')[0] if '/' in url and '-' in url else "Unknown",
            "Title": self.normalize_text(title).title(),
            "Author": self.normalize_text(author),
            "Description": self.normalize_text(desc),
            "Total_Reads": reads,
            "Total_Votes": votes,
            "Total_Parts": parts,
            "Tags": " | ".join(tags[:15]),
            "Story_URL": url
        }

    def parse_chapters_bs4(self, html_content):
        """Parses chapter list using BeautifulSoup with exact structure matching."""
        soup = BeautifulSoup(html_content, 'html.parser')
        chapters = []
        seen_urls = set()

        # METHOD 1: story-parts container
        story_parts = soup.find('ul', attrs={'aria-label': 'story-parts'})

        if story_parts:
            list_items = story_parts.find_all('li', recursive=False)
            for li in list_items:
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
                        "Order": len(chapters) + 1,
                        "Title": self.normalize_text(title),
                        "URL": full_url,
                        "Published_Date": date
                    })

        # METHOD 2: Fallback general link search
        if not chapters:
            all_links = soup.find_all('a', href=True)
            for link in all_links:
                href = link['href']
                text = link.get_text(strip=True)
                if not re.search(r'/\d+-', href):
                    continue
                if '/story/' in href:
                    continue
                if any(x in href for x in ['/user/', '/list/', '/login', '/search', '/myworks']):
                    continue
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
                        "Order": len(chapters) + 1,
                        "Title": self.normalize_text(text),
                        "URL": full_url,
                        "Published_Date": date
                    })

        return chapters

    def extract_chapter_text(self, soup):
        """
        Extracts story text from a chapter page soup object.
        MUST be called BEFORE any decompose() operations on the soup.
        Uses copy.copy(p) to avoid mutating the shared soup object.
        """
        text_content = []

        paragraphs = soup.find_all('p', attrs={'data-p-id': True})

        if paragraphs:
            for p in paragraphs:
                # Clone to avoid mutating the shared soup object
                p_copy = copy.copy(p)

                # Remove inline UI elements from the clone only
                for ui_element in p_copy.find_all(
                    ['div', 'button'],
                    class_=re.compile(r'component-wrapper|comment-marker')
                ):
                    ui_element.decompose()

                para_text = p_copy.get_text(strip=True)

                if para_text:
                    text_content.append(para_text)

        # Fallback: pre tag with nested paragraphs
        if not text_content:
            pre_tag = soup.find('pre', class_=re.compile(r'chapter-text|story-text'))
            if pre_tag:
                pre_copy = copy.copy(pre_tag)
                for ui_element in pre_copy.find_all(
                    ['div', 'button'],
                    class_=re.compile(r'component-wrapper|comment-marker')
                ):
                    ui_element.decompose()
                for p in pre_copy.find_all('p'):
                    para_text = p.get_text(strip=True)
                    if para_text:
                        text_content.append(para_text)

        return '\n\n'.join(text_content) if text_content else ""

    def scrape_chapter_stats(self, chapter_url, extract_text=False):
        """
        Visits a chapter page to get stats and optionally extract full text.
        Uses scroll-to-load instead of URL pagination to avoid duplicate text.
        Wattpad appends paginated content to the existing DOM on scroll,
        so navigating to /page/2 would include page 1 paragraphs again.
        """
        try:
            print(f"    Scanning: {chapter_url[:60]}...")
            self.driver.get(chapter_url)
            time.sleep(3)

            # ✅ STEP 1: Scroll to load ALL paginated content into the DOM
            if extract_text:
                self._scroll_to_load_full_chapter()
                # Scroll back to top so stats header elements are in view
                self.driver.execute_script("window.scrollTo(0, 0);")
                time.sleep(1)

            soup = BeautifulSoup(self.driver.page_source, 'html.parser')
            stats = {"Reads": "N/A", "Votes": "N/A", "Comments": "N/A"}

            # ✅ STEP 2: Extract text ONCE from fully-loaded soup, before any decompose()
            if extract_text:
                stats['Chapter_Text'] = self.extract_chapter_text(soup)

            # ✅ STEP 3: Noise removal for stats only (text already safely extracted)
            for noise in soup.find_all(
                ['div', 'section', 'aside'],
                class_=re.compile(r'recommend|sidebar|next-up|story-list|similar|related|footer|you-may-also')
            ):
                noise.decompose()

            for div in soup.find_all('div'):
                text = div.get_text(strip=True)
                if any(phrase in text.lower() for phrase in [
                    'you may also like', 'recommended', 'similar stories', 'more stories'
                ]):
                    if len(text) < 500:
                        div.decompose()

            # --- METHOD 1: story-stats div ---
            stats_div = soup.find('div', class_='story-stats')
            if stats_div:
                reads_span = stats_div.find('span', class_='reads')
                if reads_span:
                    title_attr = (reads_span.get('title') or
                                  reads_span.get('data-original-title') or
                                  reads_span.get('data-toggle'))
                    if title_attr:
                        reads_match = re.search(r'([\d,]+)\s+Read', title_attr, re.IGNORECASE)
                        if reads_match:
                            stats['Reads'] = reads_match.group(1).replace(',', '')

                votes_span = stats_div.find('span', class_='votes')
                if votes_span:
                    votes_match = re.search(r'([\d,]+)', votes_span.get_text(strip=True))
                    if votes_match:
                        stats['Votes'] = votes_match.group(1).replace(',', '')

                comments_span = stats_div.find('span', class_='comments')
                if comments_span:
                    comments_match = re.search(r'([\d,]+)', comments_span.get_text(strip=True))
                    if comments_match:
                        stats['Comments'] = comments_match.group(1).replace(',', '')

            # --- METHOD 2: sr-only spans ---
            if stats['Reads'] == "N/A" or stats['Votes'] == "N/A":
                main_area = soup.find('div', id='story-reading') or soup.find('article') or soup
                for span in main_area.find_all('span', class_='sr-only'):
                    text = span.get_text(strip=True)
                    reads_match    = re.search(r'Reads?\s+([\d,]+)',    text, re.IGNORECASE)
                    votes_match    = re.search(r'Votes?\s+([\d,]+)',    text, re.IGNORECASE)
                    comments_match = re.search(r'Comments?\s+([\d,]+)', text, re.IGNORECASE)
                    if reads_match    and stats['Reads']    == "N/A": stats['Reads']    = reads_match.group(1).replace(',', '')
                    if votes_match    and stats['Votes']    == "N/A": stats['Votes']    = votes_match.group(1).replace(',', '')
                    if comments_match and stats['Comments'] == "N/A": stats['Comments'] = comments_match.group(1).replace(',', '')
                    if all(v != "N/A" for v in [stats['Reads'], stats['Votes'], stats['Comments']]):
                        break

            # --- METHOD 3: data-toggle tooltip ---
            if stats['Reads'] == "N/A":
                header_section = soup.find('header') or soup.find('div', class_=re.compile(r'story-info|chapter-info'))
                search_area = header_section if header_section else soup
                for el in search_area.find_all(attrs={"data-toggle": "tooltip"}):
                    title = el.get('title', '') or el.get('data-original-title', '')
                    if 'read' in title.lower():
                        reads_match = re.search(r'([\d,]+)\s+Read', title, re.IGNORECASE)
                        if reads_match:
                            stats['Reads'] = reads_match.group(1).replace(',', '')
                            break

            # --- METHOD 4: Aria Labels ---
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
                    if all(v != "N/A" for v in [stats['Reads'], stats['Votes'], stats['Comments']]):
                        break

            # --- METHOD 5: Visible text last resort ---
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

        except Exception as e:
            print(f"      [Error extracting stats: {e}]")
            return {"Reads": "Error", "Votes": "Error", "Comments": "Error"}

    def run_local_chapter_test(self, local_file_path, extract_text=False):
        """Tests stats extraction on a single local chapter file."""
        print(f"\n{'='*70}")
        print(f"TESTING LOCAL CHAPTER: {local_file_path}")
        print(f"{'='*70}")

        abs_path = os.path.abspath(local_file_path)
        if not os.path.exists(abs_path):
            print(f"[ERROR] File not found: {abs_path}")
            return

        file_url = f"file://{abs_path}"
        stats = self.scrape_chapter_stats(file_url, extract_text=extract_text)

        print(f"\n{'='*70}")
        print("EXTRACTION RESULTS")
        print(f"{'='*70}")
        print(f"  Reads:    {stats['Reads']}")
        print(f"  Votes:    {stats['Votes']}")
        print(f"  Comments: {stats['Comments']}")

        if extract_text and 'Chapter_Text' in stats:
            print(f"\n{'='*70}")
            print("CHAPTER TEXT (First 500 characters)")
            print(f"{'='*70}")
            print(stats['Chapter_Text'][:500] + "...")
            print(f"\nTotal characters: {len(stats['Chapter_Text'])}")
            print(f"Total paragraphs: {stats['Chapter_Text'].count(chr(10)+chr(10)) + 1}")

        print(f"{'='*70}")

    def run(self, url, local_file_path=None):
        try:
            html_source = ""

            if local_file_path:
                print(f"\n{'='*70}")
                print(f"LOADING LOCAL STORY: {local_file_path}")
                print(f"{'='*70}")
                abs_path = os.path.abspath(local_file_path)
                self.driver.get(f"file://{abs_path}")
                time.sleep(5)
                html_source = self.driver.page_source
                print("✓ Local file loaded.")
            else:
                print(f"\n{'='*70}")
                print(f"SCRAPING URL: {url}")
                print(f"{'='*70}")
                self.driver.get(url)
                time.sleep(5)
                self._load_page_content()
                html_source = self.driver.page_source

            print("\nAnalyzing Metadata...")
            story_meta = self.parse_metadata_bs4(html_source, url)

            print("\nAnalyzing Chapters...")
            chapters = self.parse_chapters_bs4(html_source)
            print(f"  Found {len(chapters)} chapters.")

            if len(chapters) > 0:
                story_meta['Total_Parts'] = str(len(chapters))

            print(f"\n{'='*70}")
            print("STORY METADATA EXTRACTED")
            print(f"{'='*70}")
            print(f"  Title:        {story_meta['Title']}")
            print(f"  Author:       {story_meta['Author']}")
            print(f"  Reads:        {story_meta['Total_Reads']}")
            print(f"  Votes:        {story_meta['Total_Votes']}")
            print(f"  Parts:        {story_meta['Total_Parts']}")
            print(f"  Description:  {story_meta['Description'][:100]}...")
            print(f"  Tags:         {story_meta['Tags'][:80]}...")
            print(f"{'='*70}")

            if self.should_scrape_stats and chapters and not local_file_path:
                print(f"\n{'='*70}")
                print(f"SCRAPING STATS FOR EACH CHAPTER")
                if self.should_extract_text:
                    print("(Also extracting full chapter text via scroll-to-load)")
                print(f"{'='*70}")

                for i, chapter in enumerate(chapters, 1):
                    print(f"  [{i}/{len(chapters)}] {chapter['Title'][:35]:35}", end=" ")

                    # Restart driver every 10 chapters to avoid memory issues
                    if i > 1 and i % 10 == 0:
                        self.driver.quit()
                        self._init_driver()
                        time.sleep(1)

                    stats = self.scrape_chapter_stats(chapter['URL'], extract_text=self.should_extract_text)
                    chapter.update(stats)
                    print(f"✓ R:{stats['Reads']:>6} V:{stats['Votes']:>4} C:{stats['Comments']:>4}")

                    # Save progress every 5 chapters
                    if i % 5 == 0:
                        pd.DataFrame(chapters).to_csv("chapters_progress_v4.csv", index=False)

            print(f"\n{'='*70}")
            print("SAVING RESULTS")
            print(f"{'='*70}")

            df_story    = pd.DataFrame([story_meta])
            df_chapters = pd.DataFrame(chapters)

            suffix = "_local" if local_file_path else "_complete"
            df_story.to_csv(f"story_metadata{suffix}.csv",       index=False, encoding='utf-8-sig')
            df_chapters.to_csv(f"chapters_metadata{suffix}.csv", index=False, encoding='utf-8-sig')

            print(f"✓ Saved: story_metadata{suffix}.csv")
            print(f"✓ Saved: chapters_metadata{suffix}.csv")

            # Save individual chapter text files if text was extracted
            if self.should_extract_text and 'Chapter_Text' in df_chapters.columns:
                print(f"\nSaving individual chapter text files...")
                text_dir = "chapter_texts"
                os.makedirs(text_dir, exist_ok=True)

                saved_count = 0
                for idx, row in df_chapters.iterrows():
                    if pd.notna(row.get('Chapter_Text')) and row['Chapter_Text']:
                        title = row.get('Title', f"Chapter {idx+1}")
                        safe_title = re.sub(r'[<>:"/\\|?*]', '', title).strip()
                        if len(safe_title) > 100:
                            safe_title = safe_title[:100]
                        filename = f"{idx+1:02d} - {safe_title}.txt"
                        filepath = os.path.join(text_dir, filename)
                        with open(filepath, 'w', encoding='utf-8') as f:
                            f.write(row['Chapter_Text'])
                        saved_count += 1

                print(f"✓ Saved {saved_count} chapter text files to '{text_dir}/' folder")

            print(f"{'='*70}")
            print("✓ SCRAPING COMPLETE!")

        except Exception as e:
            print(f"\n[ERROR] {e}")
            import traceback
            traceback.print_exc()
        finally:
            if self.driver:
                self.driver.quit()


if __name__ == "__main__":
    print("="*70)
    print("WATTPAD SCRAPER V4.2 (Scroll-to-Load, No Duplicate Text)")
    print("="*70)
    print("1. Scrape Live URL (Metadata + Chapters)")
    print("2. Scrape Live URL (Metadata + Chapters + Detailed Stats)")
    print("3. Scrape Live URL (Full: Metadata + Chapters + Stats + TEXT)")
    print("4. Test on LOCAL FILE (Story Overview)")
    print("5. Test on LOCAL FILE (Single Chapter Stats)")
    print("6. Test on LOCAL FILE (Single Chapter Stats + Text)")

    choice = input("\nEnter choice (1-6): ").strip()

    url = "https://www.wattpad.com/story/353975883-homecoming"
    local_file = None
    scrape_stats = False
    extract_text = False

    if choice == '4':
        local_file = input("Enter Story .mhtml filename: ").strip().strip('"')
        if os.path.exists(local_file):
            scraper = WattpadScraperV3(headless=False, scrape_chapter_stats=False)
            scraper.run(url, local_file_path=local_file)
        else:
            print("File not found.")

    elif choice == '5':
        local_file = input("Enter Chapter .mhtml filename: ").strip().strip('"')
        if os.path.exists(local_file):
            scraper = WattpadScraperV3(headless=False, scrape_chapter_stats=True)
            scraper.run_local_chapter_test(local_file, extract_text=False)
        else:
            print("File not found.")

    elif choice == '6':
        local_file = input("Enter Chapter .mhtml filename: ").strip().strip('"')
        if os.path.exists(local_file):
            scraper = WattpadScraperV3(headless=False, scrape_chapter_stats=True, extract_chapter_text=True)
            scraper.run_local_chapter_test(local_file, extract_text=True)
        else:
            print("File not found.")

    else:
        url_input = input("Enter Wattpad story URL (press Enter for default): ").strip()
        if url_input:
            url = url_input

        if choice == '2':
            scrape_stats = True
        elif choice == '3':
            scrape_stats = True
            extract_text = True

        scraper = WattpadScraperV3(headless=False, scrape_chapter_stats=scrape_stats, extract_chapter_text=extract_text)
        scraper.run(url)