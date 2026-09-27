# nix/default.nix — Nix package for ticker-news-signals
#
# Builds the Python library + CLI as a single derivation.
# `buildPythonApplication` registers the console_scripts entry point
# from pyproject.toml and wraps the binary with the correct PYTHONPATH.
#
# gnews and vaderSentiment are provided by the flake overlay
# (nix/gnews.nix, nix/vader-sentiment.nix) because they are not packaged
# in nixpkgs; everything else comes from the python package set.

{ lib
, python
, gnews
, vaderSentiment
}:

python.pkgs.buildPythonApplication {
  pname = "ticker-news-signals";
  version = "0.1.0";
  format = "pyproject";

  # Clean source: exclude git, cache, venv, and nix from the Nix store.
  src = lib.cleanSourceWith {
    filter = path: type:
      let
        base = builtins.baseNameOf path;
      in
      base != ".git"
      && base != "__pycache__"
      && base != ".pytest_cache"
      && base != ".venv"
      && base != "nix"
      && base != ".env"
      && base != "*.log";
    src = ./..;
  };

  nativeBuildInputs = with python.pkgs; [
    setuptools
    wheel
  ];

  propagatedBuildInputs = [
    gnews
    vaderSentiment
  ];

  # Network fetch of the live GNews feed is not possible in the sandbox.
  doCheck = false;

  meta = with lib; {
    description = "Pull crypto news headlines via GNews, score sentiment with VADER, and emit per-ticker per-hour signal vectors as JSONL.";
    homepage = "https://github.com/Cairnstew/ticker-news-signals";
    license = licenses.mit;
    maintainers = [ ];
    mainProgram = "ticker-news-signals";
  };
}