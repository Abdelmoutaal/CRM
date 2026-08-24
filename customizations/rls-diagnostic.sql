-- Rôles actuels + leurs flags
SELECT label, id, "canUpdateAllSettings" AS is_admin_flag, "isEditable", "createdAt"
FROM core."role" ORDER BY "createdAt";

-- Assignations rôle ↔ utilisateurs
SELECT uw."userId", u.email, r.label AS role_label
FROM core."userWorkspace" uw
JOIN core."user" u ON u.id = uw."userId"
LEFT JOIN core."roleTarget" rt ON rt."userWorkspaceId" = uw.id
LEFT JOIN core."role" r ON r.id = rt."roleId";

-- Prédicats actuels par rôle
SELECT r.label AS role_label, om."nameSingular" AS object_name
FROM core."rowLevelPermissionPredicate" p
JOIN core."role" r ON r.id = p."roleId"
JOIN core."objectMetadata" om ON om.id = p."objectMetadataId"
WHERE p."deletedAt" IS NULL
ORDER BY r.label, om."nameSingular";
