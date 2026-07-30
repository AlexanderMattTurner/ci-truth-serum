- `release-canary`: a package absent from the npm registry is now reported as a
  missing npm marker instead of crashing with an `npm view` traceback — that
  case is exactly a first release that tagged but never published, which is the
  diagnosis the canary exists to print. Every other npm failure (auth, network,
  a registry 5xx) still fails loudly, since it says nothing about the release.
