#!/usr/bin/env bash
# Compile the Dataverse plug-in. Maintainers only: everyone else imports the
# exported solution in dynamics/solution/ and needs nothing local.
#
# Installs a private .NET SDK into .tools/ (git-ignored) on first run, and
# fetches the two Dataverse SDK assemblies the plug-in compiles against.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS="$ROOT/.tools"
export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1

if [ ! -x "$TOOLS/dotnet/dotnet" ]; then
  mkdir -p "$TOOLS"
  curl -sSL https://dot.net/v1/dotnet-install.sh -o "$TOOLS/dotnet-install.sh"
  bash "$TOOLS/dotnet-install.sh" --channel 8.0 --install-dir "$TOOLS/dotnet" --no-path
fi

mkdir -p "$TOOLS/nuget-local" "$TOOLS/sdk"
fetch() {  # id version
  local file="$TOOLS/nuget-local/$1.$2.nupkg"
  [ -f "$file" ] || curl -sSfL "https://api.nuget.org/v3-flatcontainer/$1/$2/$1.$2.nupkg" -o "$file"
}
fetch microsoft.crmsdk.coreassemblies 9.0.2.56
fetch microsoft.netframework.referenceassemblies 1.0.3
fetch microsoft.netframework.referenceassemblies.net462 1.0.3
[ -f "$TOOLS/sdk/Microsoft.Xrm.Sdk.dll" ] || unzip -o -j -q \
  "$TOOLS/nuget-local/microsoft.crmsdk.coreassemblies.9.0.2.56.nupkg" 'lib/net462/*.dll' -d "$TOOLS/sdk"

cat > "$TOOLS/nuget.config" <<EOF
<?xml version="1.0" encoding="utf-8"?>
<configuration><packageSources><clear /><add key="local" value="$TOOLS/nuget-local" /></packageSources></configuration>
EOF

cd "$ROOT/dynamics/plugin"
DOTNET_CLI_HOME="${TMPDIR:-/tmp}" NUGET_PACKAGES="$TOOLS/packages" \
  "$TOOLS/dotnet/dotnet" build -c Release --configfile "$TOOLS/nuget.config" -nologo -v q
echo "built dynamics/plugin/bin/Release/net462/Timefold.FieldService.dll"
