{
  description = "ticker-news-signals — package and dev environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      supportedSystems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" "x86_64-darwin" ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;

      # gnews and vaderSentiment are not packaged in nixpkgs, so this overlay
      # injects them into every python package set from PyPI sources
      # (see nix/gnews.nix and nix/vader-sentiment.nix).
      overlay = final: prev: {
        pythonPackagesExtensions = prev.pythonPackagesExtensions ++ [
          (pyfinal: pyprev: {
            gnews = pyfinal.callPackage ./nix/gnews.nix { };
            vaderSentiment = pyfinal.callPackage ./nix/vader-sentiment.nix { };
          })
        ];
      };

      pkgsFor = system:
        import nixpkgs {
          inherit system;
          overlays = [ overlay ];
        };
    in
    {
      # ── Packages ──────────────────────────────────────────────────────────
      # Exposes the CLI as `nix build .#ticker-news-signals`
      # and as the default package.
      packages = forAllSystems (system:
        let
          pkgs = pkgsFor system;
          pkg = pkgs.python3.pkgs.callPackage ./nix/default.nix { };
        in
        {
          default = pkg;
          ticker-news-signals = pkg;
        }
      );

      # ── Dev Shell ─────────────────────────────────────────────────────────
      # `nix develop` drops you into a shell with Python, gnews,
      # vaderSentiment, and pytest on PATH.
      devShells = forAllSystems (system:
        let
          pkgs = pkgsFor system;
          python = pkgs.python3.withPackages (ps: with ps; [
            gnews
            vaderSentiment
            pytest
          ]);
        in
        {
          default = pkgs.mkShell {
            packages = [ python ];
            shellHook = ''
              echo "ticker-news-signals dev shell"
              python -c 'import ticker_news_signals; print("ticker_news_signals:", ticker_news_signals.__version__)'
              python -c 'import gnews, vaderSentiment; print("deps: gnews, vaderSentiment")'
            '';
          };
        }
      );
    };
}