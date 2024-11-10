{
  outputs = { nixpkgs, ... }:
    let
      pkgs = import nixpkgs {
        system = "x86_64-linux";
      };
      python = let
        packageOverrides = self: super: {
          pyogg = super.pyogg.overridePythonAttrs(old: rec {
            version = "0.19.1";
            patchFlags = [
              "--binary"
              "-p1"
              "--ignore-whitespace"
            ];
            patches = with pkgs; [
              (substituteAll {
                src = ./pyogg-paths.patch;
                flacLibPath = "${flac.out}/lib/libFLAC${stdenv.hostPlatform.extensions.sharedLibrary}";
                oggLibPath = "${libogg}/lib/libogg${stdenv.hostPlatform.extensions.sharedLibrary}";
                vorbisLibPath = "${libvorbis}/lib/libvorbis${stdenv.hostPlatform.extensions.sharedLibrary}";
                vorbisFileLibPath = "${libvorbis}/lib/libvorbisfile${stdenv.hostPlatform.extensions.sharedLibrary}";
                vorbisEncLibPath = "${libvorbis}/lib/libvorbisenc${stdenv.hostPlatform.extensions.sharedLibrary}";
                opusLibPath = "${libopus}/lib/libopus${stdenv.hostPlatform.extensions.sharedLibrary}";
                opusFileLibPath = "${opusfile}/lib/libopusfile${stdenv.hostPlatform.extensions.sharedLibrary}";
              })
            ];
            src =  pkgs.fetchFromGitHub {
              owner = "TeamPyOgg";
              repo = "PyOgg";
              rev = "4118fc40067eb475468726c6bccf1242abfc24fc";
              hash = "sha256-th+qHKcDur9u4DBDD37WY62o5LR9ZUDVEFl+m7aXzNY=";
            };
          });
        };
      in pkgs.python3.override {
        inherit packageOverrides;
        self = python;
      };
      python-env = python.withPackages (ps: with ps; [
        aiohttp
        pyogg
      ]);
    in {
      devShell.x86_64-linux = pkgs.mkShell {
        buildInputs = with pkgs; [
          python-env
        ];
      };
    };
}
