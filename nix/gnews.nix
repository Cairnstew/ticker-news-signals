# nix/gnews.nix — vendored gnews derivation (not packaged in nixpkgs)
#
# Pulled from PyPI: https://pypi.org/project/gnews/
# Base runtime deps (feedparser, beautifulsoup4, dnspython, requests) are
# required at import time; the fulltext/playwright extras are intentionally
# skipped — this package only uses the RSS search surface.

{ lib
, buildPythonPackage
, fetchurl
, feedparser
, beautifulsoup4
, dnspython
, requests
}:

buildPythonPackage rec {
  pname = "gnews";
  version = "0.8.2";
  # The WHEEL, not the sdist.  gnews 0.8.2's `setup.py:3` does
  # `open('requirements.txt')` and the sdist ships no such file (only
  # `requires.txt`, which nothing reads), so `format = "setuptools"` fails
  # the build before a single byte of Python runs — which is why
  # `nix run ~/Projects/ticker-news-signals` never worked.  The wheel
  # already has the resolved metadata baked in, so there is no setup.py to
  # disagree with.
  format = "wheel";

  # `fetchPypi`'s wheel URL is the legacy PEP 503 normalised path
  # (`/packages/py2.py3/g/gnews/...`), but gnews 0.8.2 ships a py3-only
  # wheel under a content-addressed path, so that URL 404s.  fetchurl with
  # the real URL is the only spelling that resolves.
  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/97/e4/ff787e493208398d4642a656f2dcde3d95b88dcb7e31e19c837aba45e34e/gnews-0.8.2-py3-none-any.whl";
    sha256 = "KmdVwQByY9CxZIvt0toIl3IUfVFjxDZSSB9/FmOSsfA=";
  };

  propagatedBuildInputs = [
    feedparser
    beautifulsoup4
    dnspython
    requests
  ];

  # No network in the build sandbox; the package ships no test suite worth
  # running offline.
  doCheck = false;

  pythonImportsCheck = [ "gnews" ];

  meta = with lib; {
    description = "Search Google News RSS and return a usable JSON response (no API key).";
    homepage = "https://github.com/ranahaani/GNews";
    license = licenses.mit;
    maintainers = [ ];
  };
}