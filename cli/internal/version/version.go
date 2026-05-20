// Package version provides build-time version information for the CLI.
package version

import "fmt"

// Build-time variables injected via ldflags; defaults are used for local development builds.
var (
	version = "dev"
	commit  = "unknown"
	date    = "unknown"
)

// Version returns the raw version string.
func Version() string {
	return version
}

// String returns the full version string including commit and build date.
//
// The CLI is shipped as `8l` (the consumer-facing name) but the source
// repository identifies as `8l-cq` (an 8th-Layer fork of the upstream
// mozilla-ai/cq project). The fork identity is included for Apache-2.0
// attribution context — see NOTICE in this directory.
func String() string {
	return fmt.Sprintf("8l v%s (8l-cq · %s · built %s)", version, commit, date)
}
