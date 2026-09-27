# nix/vader-sentiment.nix — vendored vaderSentiment derivation (not packaged in nixpkgs)
#
# Pulled from PyPI: https://pypi.org/project/vaderSentiment/
# Pure-stdlib, dependency-free package; it ships its own lexicon so no
# runtime inputs are needed.

{ lib
, buildPythonPackage
, fetchPypi
}:

buildPythonPackage rec {
  pname = "vaderSentiment";
  version = "3.3.2";
  format = "setuptools";

  src = fetchPypi {
    inherit pname version;
    sha256 = "1y9jjs206dxqj2c00m3nz5p0d5ygf3cm63fviqirk2zw4zh0cz2x"; # 5d7c06e027fc8b99238edb0d53d970cf97066ef97654009890b83703849632f9
  };

  doCheck = false;

  pythonImportsCheck = [ "vaderSentiment" ];

  meta = with lib; {
    description = "VADER (Valence Aware Dictionary and sEntiment Reasoner) — rule-based sentiment analysis.";
    homepage = "https://github.com/cjhutto/vaderSentiment";
    license = licenses.mit;
    maintainers = [ ];
  };
}