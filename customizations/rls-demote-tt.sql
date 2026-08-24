-- Rétrograder le rôle tt en rôle non-admin
UPDATE core."role"
SET "canUpdateAllSettings" = false,
    "updatedAt" = now()
WHERE label = 'tt' AND "canUpdateAllSettings" = true;

SELECT label, "canUpdateAllSettings" FROM core."role" ORDER BY label;
