{
  description = "brutpod — RunPod GPU Deployer Webservice";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;

      packageFor = system:
        let
          pkgs = nixpkgs.legacyPackages.${system};

          pythonEnv = pkgs.python3.withPackages (ps: with ps; [
            fastapi
            uvicorn
            apscheduler
            jinja2
            requests
            python-multipart
          ]);

          appFiles = pkgs.stdenv.mkDerivation {
            pname = "brutpod-app";
            version = "0.1.0";
            src = ./.;
            installPhase = ''
              mkdir -p $out/lib/brutpod
              install -m 644 app.py $out/lib/brutpod/
              cp -r templates $out/lib/brutpod/
            '';
          };
        in
        pkgs.writeShellScriptBin "brutpod" ''
          export PYTHONPATH="${appFiles}/lib/brutpod"
          exec ${pythonEnv}/bin/uvicorn app:app \
            --host "''${BRUTPOD_HOST:-127.0.0.1}" \
            --port "''${BRUTPOD_PORT:-8080}"
        '';
    in
    {
      packages = forAllSystems (system: {
        default = packageFor system;
        brutpod  = packageFor system;
      });

      # module.nix is a curried function: defaultPkg -> NixOS module.
      # The closure over `self` lets us resolve the package lazily at eval time,
      # when pkgs.system is known.
      nixosModules.default = { config, lib, pkgs, ... }@args:
        import ./module.nix self.packages.${pkgs.system}.default args;

      nixosModules.brutpod = self.nixosModules.default;

      devShells = forAllSystems (system:
        let pkgs = nixpkgs.legacyPackages.${system}; in {
          default = pkgs.mkShell {
            packages = [
              (pkgs.python3.withPackages (ps: with ps; [
                fastapi uvicorn apscheduler jinja2 requests python-multipart
              ]))
            ];
          };
        }
      );
    };
}
