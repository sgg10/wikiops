"""``github_wiki`` provider: Markdown pages in a GitHub wiki, published through git.

The factory is the entry point (``wikiops.providers`` group); everything else in this
package is internal and wired by it.
"""

from wikiops.providers.github_wiki.factory import GithubWikiProviderFactory

__all__ = ["GithubWikiProviderFactory"]
