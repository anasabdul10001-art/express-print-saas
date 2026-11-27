// Brand colours of the public NettoDesk pages. Load right after the Tailwind CDN script.
tailwind.config = {
    theme: {
        extend: {
            colors: {
                brand: { DEFAULT: "#F48050", hover: "#F6916A", deep: "#A8431F", soft: "#FDEBE2" },
                cream: "#FCF4EC",
                ink: "#1C1413",
                line: "#EFE4D8",
            },
        },
    },
};
