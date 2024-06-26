from collections.abc import Iterable
import multiprocessing
import os
import re
from collections import Counter
import subprocess

import tantivy
from diskcache import Cache
from loguru import logger
from redis import Redis
from tqdm import tqdm

from sweepai.utils.timer import Timer
from sweepai.config.server import CACHE_DIRECTORY, FILE_CACHE_DISABLED, REDIS_URL
from sweepai.core.entities import Snippet
from sweepai.core.repo_parsing_utils import directory_to_chunks
from sweepai.core.vector_db import multi_get_query_texts_similarity
from sweepai.dataclasses.files import Document
from sweepai.logn.cache import file_cache
from sweepai.config.client import SweepConfig

token_cache = Cache(f'{CACHE_DIRECTORY}/token_cache') # we instantiate a singleton, diskcache will handle concurrency
lexical_index_cache = Cache(f'{CACHE_DIRECTORY}/lexical_index_cache')
snippets_cache = Cache(f'{CACHE_DIRECTORY}/snippets_cache')
CACHE_VERSION = "v1.0.14"

if FILE_CACHE_DISABLED:
    redis_client = None

# pylint: disable=no-member
schema_builder = tantivy.SchemaBuilder()
schema_builder.add_text_field("title", stored=True)
schema_builder.add_text_field("body", stored=True)
schema_builder.add_integer_field("doc_id", stored=True)
schema = schema_builder.build()
# pylint: enable=no-member

redis_client = Redis.from_url(REDIS_URL) if REDIS_URL else None


class CustomIndex:
    def __init__(self, cache_path: str = None):
        os.makedirs(cache_path, exist_ok=True)
        self.index = tantivy.Index(schema) # pylint: disable=no-member
    
    def add_documents(self, documents: Iterable):
        writer = self.index.writer()
        for doc_id, (title, text) in enumerate(documents):
            writer.add_document(
                tantivy.Document( # pylint: disable=no-member
                    title=title,
                    body=text,
                    doc_id=doc_id
                )
            )
        writer.commit()
    
    def search_index(self, query: str) -> list[tuple[str, float, dict]]:
        query = tokenize_code(query)
        query = self.index.parse_query(query)
        searcher = self.index.searcher()
        results = searcher.search(query, limit=200).hits
        return [(searcher.doc(doc_id)["title"][0], score, searcher.doc(doc_id)) for score, doc_id in results]


variable_pattern = re.compile(r"([A-Z][a-z]+|[a-z]+|[A-Z]+(?=[A-Z]|$))")


def tokenize_code(code: str) -> list[str]:
    matches = re.finditer(r"\b\w{2,}\b", code)
    tokens = []
    for m in matches:
        text = m.group()

        for section in text.split("_"):
            for part in variable_pattern.findall(section):
                if len(part) < 2:
                    continue
                # if len(part) < 5:
                #     tokens.append(part.lower())
                # if more than half of the characters are letters 
                # and the ratio of unique characters to the number of characters is less than 5
                if sum(1 for c in part if 'a' <= c <= 'z' or 'A' <= c <= 'Z' or '0' <= c <= '9') > len(part) // 2 \
                    and len(part) / len(set(part)) < 4:
                    tokens.append(part.lower())
    
    return " ".join(tokens)

def compute_document_tokens(
    content: str,
) -> tuple[Counter, int]:  # method that offloads the computation to a separate process
    results = token_cache.get(content + CACHE_VERSION)
    if results is not None:
        return results
    tokens = tokenize_code(content)
    token_cache[content] = tokens
    return tokens

def snippets_to_docs(snippets: list[Snippet], len_repo_cache_dir):
    docs = []
    for snippet in snippets:
        docs.append(
            Document(
                title=f"{snippet.file_path[len_repo_cache_dir:]}:{snippet.start}-{snippet.end}",
                content=snippet.get_snippet(add_ellipsis=False, add_lines=False),
            )
        )
    return docs


@file_cache(ignore_params=["ticket_progress", "len_repo_cache_dir"])
def prepare_index_from_snippets(
    snippets: list[Snippet],
    len_repo_cache_dir: int = 0,
    do_not_use_file_cache: bool = False,
    cache_path: str = None,
) -> CustomIndex | None:
    all_docs: list[Document] = snippets_to_docs(snippets, len_repo_cache_dir)
    if len(all_docs) == 0:
        return None
    index = CustomIndex(
        cache_path=cache_path
    )
    all_tokens = []
    workers = multiprocessing.cpu_count() // 2
    try:
        if workers > 1:
            with multiprocessing.Pool(processes=multiprocessing.cpu_count() // 2) as p:
                all_tokens = p.map(
                    compute_document_tokens,
                    tqdm(
                        [doc.content for doc in all_docs],
                        total=len(all_docs),
                        desc="Tokenizing documents"
                    )
                )
        else:
            all_tokens = zip(
                *[
                    compute_document_tokens(doc.content)
                    for doc in tqdm(all_docs, desc="Tokenizing documents")
                ]
            )
        all_titles = [doc.title for doc in all_docs]
        with Timer():
            index.add_documents(
                tqdm(zip(all_titles, all_tokens), total=len(all_docs), desc="Indexing")
            )
    except FileNotFoundError as e:
        logger.exception(e)

    return index


def search_index(query, index: CustomIndex):
    """Search the index based on a query.

    This function takes a query and an index as input and returns a dictionary of document IDs
    and their corresponding scores.
    """
    if index is None:
        return {}
    try:
        # Create a query parser for the "content" field of the index
        results_with_metadata = index.search_index(query)
        # Search the index
        res = {}
        for doc_id, score, _ in results_with_metadata:
            if doc_id not in res:
                res[doc_id] = score
        # min max normalize scores from 0.5 to 1
        if len(res) == 0:
            max_score = 1
            min_score = 0
        else:
            max_score = max(res.values())
            min_score = min(res.values()) if min(res.values()) < max_score else 0
        res = {k: (v - min_score) / (max_score - min_score) for k, v in res.items()}
        return res
    except Exception as e:
        logger.exception(e)
        return {}

SNIPPET_FORMAT = """File path: {file_path}

{contents}"""

# @file_cache(ignore_params=["snippets"])
def compute_vector_search_scores(queries: list[str], snippets: list[Snippet]):
    # get get dict of snippet to score
    with Timer() as timer:
        snippet_str_to_contents = {
            snippet.denotation: SNIPPET_FORMAT.format(
                file_path=snippet.file_path,
                contents=snippet.get_snippet(add_ellipsis=False, add_lines=False),
            )
            for snippet in snippets
        }
    logger.info(f"Snippet to contents took {timer.time_elapsed:.2f} seconds")
    snippet_contents_array = list(snippet_str_to_contents.values())
    multi_query_snippet_similarities = multi_get_query_texts_similarity(
        queries, snippet_contents_array
    )
    snippet_denotations = [snippet.denotation for snippet in snippets]
    snippet_denotation_to_scores = [{
        snippet_denotations[i]: score
        for i, score in enumerate(query_snippet_similarities)
    } for query_snippet_similarities in multi_query_snippet_similarities]
    return snippet_denotation_to_scores

def get_lexical_cache_key(repo_directory: str, commit_hash: str | None = None):
    commit_hash = commit_hash or subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_directory, capture_output=True, text=True).stdout.strip()
    repo_directory = os.path.basename(repo_directory)
    return f"{repo_directory}_{commit_hash}_{CACHE_VERSION}"

@file_cache(ignore_params=["sweep_config", "ticket_progress"])
def prepare_lexical_search_index(
    repo_directory: str,
    sweep_config: SweepConfig,
    do_not_use_file_cache: bool = False # choose to not cache results
):
    lexical_cache_key = get_lexical_cache_key(repo_directory)

    snippets_results = snippets_cache.get(lexical_cache_key)
    if snippets_results is None:
        snippets, file_list = directory_to_chunks(
            repo_directory, sweep_config, do_not_use_file_cache=do_not_use_file_cache
        )
        snippets_cache[lexical_cache_key] = snippets, file_list
    else:
        snippets, file_list = snippets_results

    index = prepare_index_from_snippets(
        snippets,
        len_repo_cache_dir=len(repo_directory) + 1,
        do_not_use_file_cache=do_not_use_file_cache,
        cache_path=f"{CACHE_DIRECTORY}/lexical_index_cache/{lexical_cache_key}"
    )

    return file_list, snippets, index


if __name__ == "__main__":
    repo_directory = os.getenv("REPO_DIRECTORY")
    sweep_config = SweepConfig()
    assert repo_directory
    import time
    start = time.time()
    _, _ , index = prepare_lexical_search_index(repo_directory, sweep_config, None)
    result = search_index("logger export", index)
    print("Time taken:", time.time() - start)
    # print some of the keys
    print(list(result.keys())[:5])
    # print the first 2 result keys sorting by value
    print(sorted(result.items(), key=lambda x: result.get(x, 0), reverse=True)[:5])
