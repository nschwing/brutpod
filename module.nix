# Called as: import ./module.nix <defaultPackage> { config, lib, pkgs, ... }
defaultPkg:
{ config, lib, pkgs, ... }:
let
  cfg = config.services.brutpod;
in {
  options.services.brutpod = {
    enable = lib.mkEnableOption "brutpod RunPod GPU deployer web service";

    package = lib.mkOption {
      type = lib.types.package;
      default = defaultPkg;
      defaultText = lib.literalExpression "brutpod from flake";
      description = "The brutpod package to use.";
    };

    host = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "Host address to bind to. Use 0.0.0.0 to listen on all interfaces.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8080;
      description = "TCP port to listen on.";
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      example = lib.literalExpression "config.sops.secrets.brutpod_env.path";
      description = ''
        Path to a file with secret environment variables (KEY=VALUE, one per line):
          RUNPOD_API_KEY=...
          PUSHOVER_TOKEN=...
          PUSHOVER_USER=...
        These take precedence over values configured via the web UI.
        With sops-nix, set sops.secrets.<name>.owner = "brutpod".
      '';
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Open the service port in the firewall.";
    };
  };

  config = lib.mkIf cfg.enable {
    users.users.brutpod = {
      isSystemUser = true;
      group = "brutpod";
      description = "brutpod service user";
    };
    users.groups.brutpod = {};

    systemd.services.brutpod = {
      description = "brutpod RunPod GPU deployer";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];

      serviceConfig = lib.mkMerge [
        {
          ExecStart = "${cfg.package}/bin/brutpod";
          WorkingDirectory = "/var/lib/brutpod";
          StateDirectory = "brutpod";
          StateDirectoryMode = "0750";
          User = "brutpod";
          Group = "brutpod";
          Environment = [
            "BRUTPOD_HOST=${cfg.host}"
            "BRUTPOD_PORT=${toString cfg.port}"
          ];
          Restart = "on-failure";
          RestartSec = "5s";
          NoNewPrivileges = true;
          ProtectSystem = "strict";
          ProtectHome = true;
          PrivateTmp = true;
          PrivateDevices = true;
        }
        (lib.mkIf (cfg.environmentFile != null) {
          EnvironmentFile = cfg.environmentFile;
        })
      ];
    };

    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];
  };
}
