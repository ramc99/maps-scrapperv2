module.exports = {
  apps: [
    {
      name: "rc-maps",
      script: "/usr/bin/python3",
      args: "phase2_extractor.py --maps rancho_cucamonga",
      autorestart: false,
      watch: false,
    },
    {
      name: "rc-web",
      script: "/usr/bin/python3",
      args: "phase2_extractor.py --website rancho_cucamonga",
      autorestart: false,
      watch: false,
    },
    {
      name: "maps-phase2",
      script: "/usr/bin/python3",
      args: "phase2_extractor.py --maps",
      autorestart: false,
      watch: false,
    },
    {
      name: "maps-extraction",
      script: "/usr/bin/python3",
      args: "phase2_extractor.py --website",
      autorestart: false,
      watch: false,
    },
  ],
};
