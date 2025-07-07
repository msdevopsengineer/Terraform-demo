# PUBLISH VLUNERABILITES RESULTS LIKE TEST CASE RESULTS IN AZ PIPELINE

<?xml version="1.0" encoding="UTF-8"?>
<testsuites>
  {{- range . }}
    {{- $target := .Target }}
    <testsuite name="Trivy Scan - {{ $target }}">
      {{- range .Vulnerabilities }}
        <testcase classname="{{ $target }}" name="{{ .VulnerabilityID }} - {{ .PkgName }}">
          <failure type="{{ .Severity }}">
            <![CDATA[
Vulnerability: {{ .VulnerabilityID }}
Package: {{ .PkgName }}
Installed Version: {{ .InstalledVersion }}
Fixed Version: {{ .FixedVersion }}
Severity: {{ .Severity }}
Title: {{ .Title }}
Description: {{ .Description }}
References:
  {{- range .References }}
    - {{ . }}
  {{- end }}
            ]]>
          </failure>
        </testcase>
      {{- end }}
    </testsuite>
  {{- end }}
</testsuites>
