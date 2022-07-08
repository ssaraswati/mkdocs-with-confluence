import time
import os
import sys
import re
import tempfile
import shutil
import requests
import mimetypes
import mistune
import contextlib
from time import sleep
from mkdocs.config import config_options
from mkdocs.plugins import BasePlugin
from md2cf.confluence_renderer import ConfluenceRenderer
from md2cf.api import MinimalConfluence
from os import environ

TEMPLATE_BODY = "<p> TEMPLATE </p>"


@contextlib.contextmanager
def nostdout():
    save_stdout = sys.stdout
    sys.stdout = DummyFile()
    yield
    sys.stdout = save_stdout


class DummyFile(object):
    def write(self, x):
        pass


class MkdocsWithConfluence(BasePlugin):
    _id = 0
    config_scheme = (
        ("host_url", config_options.Type(str, default=None)),
        ("space", config_options.Type(str, default=None)),
        ("parent_page_name", config_options.Type(str, default=None)),
        ("username", config_options.Type(str, default=environ.get("JIRA_USERNAME", None))),
        ("password", config_options.Type(str, default=environ.get("JIRA_PASSWORD", None))),
        ("enabled_if_env", config_options.Type(str, default=None)),
        ("verbose", config_options.Type(bool, default=False)),
        ("debug", config_options.Type(bool, default=False)),
        ("dryrun", config_options.Type(bool, default=False)),
    )

    def __init__(self):
        self.enabled = True
        self.confluence_renderer = ConfluenceRenderer(use_xhtml=True)
        self.confluence_mistune = mistune.Markdown(renderer=self.confluence_renderer)
        self.simple_log = False
        self.flen = 1


    def on_nav(self, nav, config, files):
        MkdocsWithConfluence.tab_nav = []
        navigation_items = nav.__repr__()

        for n in navigation_items.split("\n"):
            leading_spaces = len(n) - len(n.lstrip(" "))
            spaces = leading_spaces * " "
            if "Page" in n:
                try:
                    self.page_title = self.__get_page_title(n)
                    if self.page_title is None:
                        raise AttributeError
                except AttributeError:
                    self.page_local_path = self.__get_page_url(n)
                    print(
                        f"WARN    - Page from path {self.page_local_path} has no"
                        f"          entity in the mkdocs.yml nav section. It will be uploaded"
                        f"          to the Confluence, but you may not see it on the web server!"
                    )
                    self.page_local_name = self.__get_page_name(n)
                    self.page_title = self.page_local_name

                p = spaces + self.page_title
                MkdocsWithConfluence.tab_nav.append(p)
            if "Section" in n:
                try:
                    self.section_title = self.__get_section_title(n)
                    if self.section_title is None:
                        raise AttributeError
                except AttributeError:
                    self.section_local_path = self.__get_page_url(n)
                    print(
                        f"WARN    - Section from path {self.section_local_path} has no"
                        f"          entity in the mkdocs.yml nav section. It will be uploaded"
                        f"          to the Confluence, but you may not see it on the web server!"
                    )
                    self.section_local_name = self.__get_section_title(n)
                    self.section_title = self.section_local_name
                s = spaces + self.section_title
                MkdocsWithConfluence.tab_nav.append(s)

    def on_files(self, files, config):
        pages = files.documentation_pages()
        try:
            self.flen = len(pages)
            print(f"Number of Files in directory tree: {self.flen}")
        except 0:
            print("ERR: You have no documentation pages" "in the directory tree, please add at least one!")

    def on_post_template(self, output_content, template_name, config):
        if self.config["verbose"] is False and self.config["debug"] is False:
            self.simple_log = True
            print("INFO    -  Mkdocs With Confluence: Start exporting markdown pages... (simple logging)")
        else:
            self.simple_log = False

    def on_config(self, config):
        if "enabled_if_env" in self.config:
            env_name = self.config["enabled_if_env"]
            if env_name:
                self.enabled = os.environ.get(env_name) == "1"
                if not self.enabled:
                    print(
                        "WARNING - Mkdocs With Confluence: Exporting MKDOCS pages to Confluence turned OFF: "
                        f"(set environment variable {env_name} to 1 to enable)"
                    )
                    return
                else:
                    print(
                        "INFO    -  Mkdocs With Confluence: Exporting MKDOCS pages to Confluence "
                        f"turned ON by var {env_name}==1!"
                    )
                    self.enabled = True
            else:
                print(
                    "WARNING -  Mkdocs With Confluence: Exporting MKDOCS pages to Confluence turned OFF: "
                    f"(set environment variable {env_name} to 1 to enable)"
                )
                return
        else:
            print("INFO    -  Mkdocs With Confluence: Exporting MKDOCS pages to Confluence turned ON by default!")
            self.enabled = True

        if self.config["dryrun"]:
            print("WARNING -  Mkdocs With Confluence - DRYRUN MODE turned ON")
            self.dryrun = True
        else:
            self.dryrun = False

    def on_page_markdown(self, markdown, page, config, files):
        MkdocsWithConfluence._id += 1
        self.pw = self.config["password"]
        self.user = self.config["username"]
        self.confluence = MinimalConfluence(host=self.config["host_url"].strip("/content"), username=self.config["username"], password=self.config["password"])

        if self.enabled:
            if self.simple_log is True:
                print("INFO    - Mkdocs With Confluence: Page export progress: [", end="", flush=True)
                for i in range(MkdocsWithConfluence._id):
                    print("#", end="", flush=True)
                for j in range(self.flen - MkdocsWithConfluence._id):
                    print("-", end="", flush=True)
                print(f"] ({MkdocsWithConfluence._id} / {self.flen})", end="\r", flush=True)

            if self.config["debug"]:
                print(f"\nDEBUG    - Handling Page '{page.title}' (And Parent Nav Pages if necessary):\n")
            if not all(self.config_scheme):
                print("DEBUG    - ERR: YOU HAVE EMPTY VALUES IN YOUR CONFIG. ABORTING")
                return markdown

            try:
                if self.config["debug"]:
                    print("DEBUG    - Get section first parent title...: ")
                try:

                    parent = self.__get_section_title(page.ancestors[0].__repr__())
                except IndexError as e:
                    if self.config["debug"]:
                        print(
                            f"DEBUG    - WRN({e}): No first parent! Assuming "
                            f"DEBUG    - {self.config['parent_page_name']}..."
                        )
                    parent = None
                if self.config["debug"]:
                    print(f"DEBUG    - {parent}")
                if not parent:
                    parent = self.config["parent_page_name"]

                if self.config["parent_page_name"] is not None:
                    main_parent = self.config["parent_page_name"]
                else:
                    main_parent = self.config["space"]

                if self.config["debug"]:
                    print("DEBUG    - Get section second parent title...: ")
                try:
                    parent1 = self.__get_section_title(page.ancestors[1].__repr__())
                except IndexError as e:
                    if self.config["debug"]:
                        print(
                            f"DEBUG    - ERR({e}) No second parent! Assuming "
                            f"second parent is main parent: {main_parent}..."
                        )
                    parent1 = None
                if self.config["debug"]:
                    print(f"{parent}")

                if not parent1:
                    parent1 = main_parent
                    if self.config["debug"]:
                        print(
                            f"DEBUG    - ONLY ONE PARENT FOUND. ASSUMING AS A "
                            f"FIRST NODE after main parent config {main_parent}"
                        )

                if self.config["debug"]:
                    print(f"DEBUG    - PARENT0: {parent}, PARENT1: {parent1}, MAIN PARENT: {main_parent}")

                tf = tempfile.NamedTemporaryFile(delete=False)
                f = open(tf.name, "w")

                files = []
                try:
                    for match in re.finditer(r'img src="file://(.*)" s', markdown):
                        if self.config["debug"]:
                            print(f"DEBUG    - FOUND IMAGE: {match.group(1)}")
                        files.append(match.group(1))
                except AttributeError as e:
                    if self.config["debug"]:
                        print(f"DEBUG    - WARN(({e}): No images found in markdown. Proceed..")

                new_markdown = re.sub(
                    r'<img src="file:///tmp/', '<p><ac:image ac:height="350"><ri:attachment ri:filename="', markdown
                )
                new_markdown = re.sub(r'" style="page-break-inside: avoid;">', '"/></ac:image></p>', new_markdown)
                confluence_body = self.confluence_mistune(new_markdown)
                f.write(confluence_body)
                if self.config["debug"]:
                    print(confluence_body)
                page_name = page.title
                new_name = "confluence_page_" + page_name.replace(" ", "_") + ".html"
                shutil.copy(f.name, new_name)
                f.close()

                if self.config["debug"]:
                    print(
                        f"\nDEBUG    - UPDATING PAGE TO CONFLUENCE, DETAILS:\n"
                        f"DEBUG    - HOST: {self.config['host_url']}\n"
                        f"DEBUG    - SPACE: {self.config['space']}\n"
                        f"DEBUG    - TITLE: {page.title}\n"
                        f"DEBUG    - PARENT: {parent}\n"
                        f"DEBUG    - BODY: {confluence_body}\n"
                    )

                confluence_page = self.find_page(page.title)
                if confluence_page is not None:
                    if self.config["debug"]:
                        print(
                            f"DEBUG    - JUST ONE STEP FROM UPDATE OF PAGE '{page.title}' \n"
                            f"DEBUG    - CHECKING IF PARENT PAGE ON CONFLUENCE IS THE SAME AS HERE"
                        )

                    parent_name = self.find_parent_name_of_page(page.title)

                    if parent_name == parent:
                        if self.config["debug"]:
                            print("DEBUG    - Parents match. Continue...")
                    else:
                        if self.config["debug"]:
                            print(f"DEBUG    - ERR, Parents does not match: '{parent}' =/= '{parent_name}' Aborting...")
                        return markdown
                    self.update_page(page.title, confluence_body)
                    for i in MkdocsWithConfluence.tab_nav:
                        if page.title in i:
                            n_kol = len(i + " *NEW PAGE*")
                            print(f"INFO    - Mkdocs With Confluence: {i} *UPDATE*")
                else:
                    if self.config["debug"]:
                        print(
                            f"DEBUG    - PAGE: {page.title}, PARENT0: {parent}, "
                            f"PARENT1: {parent1}, MAIN PARENT: {main_parent}"
                        )
                    parent_page = self.find_page(parent)
                    self.wait_until(parent_page, 1, 20)
                    second_parent_page = self.find_page(parent1)
                    self.wait_until(second_parent_page, 1, 20)
                    main_parent_page = self.find_page(main_parent)
                    if not parent_page:
                        if not second_parent_page:
                            main_parent_page = self.find_page(main_parent)
                            if not main_parent_page:
                                print("ERR: MAIN PARENT UNKNOWN. ABORTING!")
                                return markdown
                            main_parent_id = main_parent_page["id"]
                            if self.config["debug"]:
                                
                                print(
                                    f"DEBUG    - Trying to ADD page '{parent1}' to "
                                    f"main parent({main_parent}) ID: {main_parent_id}"
                                )
                            body = TEMPLATE_BODY.replace("TEMPLATE", parent1)
                            self.add_page(parent1, main_parent_id, body)
                            for i in MkdocsWithConfluence.tab_nav:
                                if parent1 in i:
                                    n_kol = len(i + "INFO    - Mkdocs With Confluence:" + " *NEW PAGE*")
                                    print(f"INFO    - Mkdocs With Confluence: {i} *NEW PAGE*")
                            time.sleep(1)

                        if self.config["debug"]:
                            print(
                                f"DEBUG    - Trying to ADD page '{parent}' "
                                f"to parent1({parent1}) ID: {second_parent_page}"
                            )
                        body = TEMPLATE_BODY.replace("TEMPLATE", parent)
                        self.add_page(parent, second_parent_page, body)
                        for i in MkdocsWithConfluence.tab_nav:
                            if parent in i:
                                n_kol = len(i + "INFO    - Mkdocs With Confluence:" + " *NEW PAGE*")
                                print(f"INFO    - Mkdocs With Confluence: {i} *NEW PAGE*")
                        time.sleep(1)

                    # if self.config['debug']:

                    if parent_page is None:
                        for i in range(11):
                            while parent_page is None:
                                try:
                                    self.add_page(page.title, parent_page["id"], confluence_body)
                                except requests.exceptions.HTTPError:
                                    print(
                                        f"ERR    - HTTP error on adding page. It probably occured due to "
                                        f"parent ID('{parent_page.id}') page is not YET synced on server. Retry nb {i}/10..."
                                    )
                                    sleep(5)
                                    parent_page = self.find_page(parent)
                                break

                    self.add_page(page.title, parent_page["id"], confluence_body)

                    print(f"Trying to ADD page '{page.title}' to parent0({parent}) ID: {parent_page.id}")
                    for i in MkdocsWithConfluence.tab_nav:
                        if page.title in i:
                            n_kol = len(i + "INFO    - Mkdocs With Confluence:" + " *NEW PAGE*")
                            print(f"INFO    - Mkdocs With Confluence: {i} *NEW PAGE*")

                if files:
                    if self.config["debug"]:
                        print(f"\nDEBUG    - UPLOADING ATTACHMENTS TO CONFLUENCE, DETAILS:\n" f"FILES: {files}\n")

                    n_kol = len("  *NEW ATTACHMENTS({len(files)})*")
                    print(f"\033[A\033[F\033[{n_kol}G  *NEW ATTACHMENTS({len(files)})*")
                    for f in files:
                        self.add_attachment(page.title, f)

            except IndexError as e:
                if self.config["debug"]:
                    print(f"DEBUG    - ERR({e}): Exception error!")
                return markdown

        return markdown

    def on_page_content(self, html, page, config, files):
        return html

    def __get_page_url(self, section):
        return re.search("url='(.*)'\\)", section).group(1)[:-1] + ".md"

    def __get_page_name(self, section):
        return os.path.basename(re.search("url='(.*)'\\)", section).group(1)[:-1])

    def __get_section_name(self, section):
        if self.config["debug"]:
            print(f"DEBUG    - SECTION name: {section}")
        return os.path.basename(re.search("url='(.*)'\\/", section).group(1)[:-1])

    def __get_section_title(self, section):
        if self.config["debug"]:
            print(f"DEBUG    - SECTION title: {section}")
        try:
            r = re.search("Section\\(title='(.*)'\\)", section)
            return r.group(1)
        except AttributeError:
            name = self.__get_section_name(section)
            print(f"WRN    - Section '{name}' doesn't exist in the mkdocs.yml nav section!")
            return name

    def __get_page_title(self, section):
        try:
            r = re.search("\\s*Page\\(title='(.*)',", section)
            return r.group(1)
        except AttributeError:
            name = self.__get_page_url(section)
            print(f"WRN    - Page '{name}' doesn't exist in the mkdocs.yml nav section!")
            return name

    def add_attachment(self, page_name, filepath):
        print(f"INFO    - Mkdocs With Confluence * {page_name} *NEW ATTACHMENT* {filepath}")
        if self.config["debug"]:
            print(f" * Mkdocs With Confluence: Add Attachment: PAGE NAME: {page_name}, FILE: {filepath}")
        page = self.find_page(page_name)
        if page:
            url = self.config["host_url"] + "/" + page["id"] + "/child/attachment/"
            headers = {"X-Atlassian-Token": "no-check"}  # no content-type here!
            if self.config["debug"]:
                print(f"URL: {url}")
            filename = filepath
            auth = (self.user, self.pw)

            # determine content-type
            content_type, encoding = mimetypes.guess_type(filename)
            if content_type is None:
                content_type = "multipart/form-data"
            files = {"file": (filename, open(filename, "rb"), content_type)}

            if not self.dryrun:
                r = requests.post(url, headers=headers, files=files, auth=auth)
                r.raise_for_status()
                if r.status_code == 200:
                    print("OK!")
                else:
                    print("ERR!")
        else:
            if self.config["debug"]:
                print("PAGE DOES NOT EXISTS")

    def find_page_id(self, page_name):
        page = self.find_page(page_name)
        if page:
            if self.config["debug"]:
                print(f"ID: {page['id']}")
            return page["id"]
        else:
            if self.config["debug"]:
                print("PAGE DOES NOT EXIST")
            return None

    def find_page(self, page_name, ancestors=False):
        if self.config["debug"]:
            print(f"INFO    -   * Mkdocs With Confluence: Find Page: PAGE NAME: {page_name}")
        expansions = ["ancestors"] if ancestors else []
        page = self.confluence.get_page(title=page_name, space_key=self.config["space"], additional_expansions=expansions)
        if page:
            if self.config["debug"]:
                print(f"ID: {page['id']}")
            return page
        else:
            if self.config["debug"]:
                print("PAGE DOES NOT EXIST")
            return None

    def add_page(self, page_name, parent_page_id, page_content_in_storage_format):
        print(f"INFO    -   * Mkdocs With Confluence: {page_name} - *NEW PAGE*")

        if self.config["debug"]:
            print(f" * Mkdocs With Confluence: Adding Page: PAGE NAME: {page_name}, parent ID: {parent_page_id}")

        if not self.dryrun:
            self.confluence.create_page(self.config["space"], page_name, page_content_in_storage_format, parent_page_id)

    def update_page(self, page_name, page_content_in_storage_format):
        page = self.find_page(page_name)
        print(f"INFO    -   * Mkdocs With Confluence: {page_name} - *UPDATE*")
        if self.config["debug"]:
            print(f" * Mkdocs With Confluence: Update PAGE ID: {page.id}, PAGE NAME: {page_name}")
        if page:
            if not self.dryrun:
                self.confluence.update_page(page, page_content_in_storage_format)
        else:
            if self.config["debug"]:
                print("PAGE DOES NOT EXIST YET!")


    def find_parent_name_of_page(self, name):
        if self.config["debug"]:
            print(f"INFO    -   * Mkdocs With Confluence: Find PARENT OF PAGE, PAGE NAME: {name}")
        page = self.find_page(name, ancestors=True)
        url = self.config["host_url"] + "/" + page["id"] + "?expand=ancestors"

        auth = (self.user, self.pw)
        r = requests.get(url, auth=auth)
        r.raise_for_status()
        with nostdout():
            response_json = r.json()
        print("ancestors")
        print(response_json)
        print("page")
        print(page)
        if response_json:
            if self.config["debug"]:
                print(f"PARENT NAME: {response_json['ancestors'][-1]['title']}")
            return response_json["ancestors"][-1]["title"]
        else:
            if self.config["debug"]:
                print("PAGE DOES NOT HAVE PARENT")
            return None

    def wait_until(self, condition, interval=0.1, timeout=1):
        start = time.time()
        while not condition and time.time() - start < timeout:
            time.sleep(interval)
