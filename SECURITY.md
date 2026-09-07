# Security Policy

## Reporting a Vulnerability

We take security seriously and appreciate your help in reporting vulnerabilities responsibly.

### How to Report

🔒 **Please do NOT create a public GitHub issue** for security vulnerabilities.

Instead, please report security issues by:

1. **GitHub Security Advisory**: Use GitHub's [Security Advisory](https://github.com/noomrachit/Solarlit/security/advisories) feature
2. **Email**: Contact us with vulnerability details (if email is provided)
3. **GitHub Issues**: For non-sensitive security concerns only

### What to Include

When reporting a vulnerability, please provide:

- **Description**: Clear description of the vulnerability
- **Location**: Affected code path or component
- **Proof of Concept**: Steps to reproduce the issue
- **Impact**: Potential impact and severity
- **Suggested Fix**: If you have one

### Response Timeline

- **Initial Response**: Within 24-48 hours
- **Assessment**: Within 1 week
- **Fix & Release**: Timeline depends on severity

## Security Best Practices

### For Contributors

- ✅ Use strong, unique passwords and 2FA
- ✅ Keep dependencies up to date
- ✅ Review security alerts from Dependabot
- ✅ Follow secure coding practices
- ✅ Never commit sensitive data (keys, tokens, passwords)

### For Maintainers

- 🔐 Enable branch protection on `main`
- 🤖 Enable Dependabot for dependency monitoring
- 📝 Keep SECURITY.md updated
- 🔑 Rotate secrets and tokens regularly
- 🧪 Run security checks in CI/CD

## Security Features

This repository includes:

- 🔄 **Dependabot**: Automated dependency updates
- 🧪 **CI/CD Tests**: Automated testing on all PRs
- 🛡️ **Branch Protection**: Enforced code review requirements
- 🔐 **Type Safety**: TypeScript for compile-time safety

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| Latest  | ✅ Yes             |
| Previous| ⚠️ Limited Support |
| Older   | ❌ No              |

## Dependencies

We regularly update and monitor dependencies for known vulnerabilities using:

- [Dependabot](https://dependabot.com)
- [GitHub Security Advisories](https://github.com/advisories)
- Manual npm audit reviews

## Contact

For security-related questions, please reach out through:
- GitHub Security Advisory
- Repository Owner: [@noomrachit](https://github.com/noomrachit)

---

**Last Updated**: $(date)
**Version**: 1.0.0
