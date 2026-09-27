# nix/gnews.nix — vendored gnews derivation (not packaged in nixpkgs)
#
# Pulled from PyPI: https://pypi.org/project/gnews/
# Base runtime deps (feedparser, beautifulsoup4, dnspython, requests) are
# required at import time; the fulltext/playwright extras are intentionally
# skipped — this package only uses the RSS search surface.

{ lib
, buildPythonPackage
, fetchPypi
, feedparser
, beautifulsoup4
, dnspython
, requests
}:

buildPythonPackage rec {
  pname = "gnews";
  version = "0.8.2";
  format = "setuptools";

  src = fetchPypi {
    inherit pname version;
    sha256 = "1q14q92svyf21yb1w9p43lw6hnq8wvr4x21sfmvdlg1kcm94sqdj"; # b2614d5265333cda76753a884ef2e6085b68381de4261e960fc2f9ad45c224e0
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