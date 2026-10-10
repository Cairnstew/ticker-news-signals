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
      # `nix develop` drops you into a shell with Python, the runtime deps,
      # pytest, notebook tooling (build/validate the pipeline.ipynb and power
      # the Zed kernel), and the lint tool on PATH.
      devShells = forAllSystems (system:
        let
          pkgs = pkgsFor system;
          python = pkgs.python3.withPackages (ps: with ps; [
            gnews
            vaderSentiment
            httpx
            feedparser
            pytest
            # Notebook + kernel support (tools/build_notebook.py,
            # tools/validate_notebook.py, tools/install_kernel.py).
            nbformat
            nbclient
            ipykernel
          ]);
        in
        {
          default = pkgs.mkShell {
            packages = [
              python
              pkgs.ruff
            ];
            shellHook = ''
              echo "ticker-news-signals dev shell"
              python -c 'import ticker_news_signals; print("ticker_news_signals:", ticker_news_signals.__version__)'
              python -c 'import gnews, vaderSentiment, httpx, feedparser; print("deps: gnews, vaderSentiment, httpx, feedparser")'

              # Register the notebook kernel Zed looks for, once. Re-run
              # `python tools/install_kernel.py` after recreating the venv.
              if [ ! -e "$HOME/.local/share/jupyter/kernels/ticker-news-signals" ]; then
                python tools/install_kernel.py >/dev/null 2>&1 || true
              fi
            '';
          };
        }
      );

      # ── Apps ──────────────────────────────────────────────────────────────
      # `nix run .#kernel` (re)installs the notebook kernel; `nix run .#notebook`
      # executes the pipeline notebook against the live feeds and fails on errors.
      apps = forAllSystems (system:
        let
          inherit (pkgsFor system) pkgs;
          python = pkgs.python3.withPackages (ps: with ps; [
            gnews
            vaderSentiment
            httpx
            feedparser
            nbformat
            nbclient
            ipykernel
          ]);
        in
        {
          kernel = {
            type = "app";
            program = "${pkgs.writeShellApplication {
              name = "ticker-news-signals-install-kernel";
              runtimeInputs = [ python ];
              text = ''
                python tools/install_kernel.py
              '';
            }}/bin/ticker-news-signals-install-kernel";
          };

          notebook = {
            type = "app";
            program = "${pkgs.writeShellApplication {
              name = "ticker-news-signals-notebook";
              runtimeInputs = [ python ];
              text = ''
                exec python tools/validate_notebook.py "$@"
              '';
            }}/bin/ticker-news-signals-notebook";
          };
        }
      );
    };
}