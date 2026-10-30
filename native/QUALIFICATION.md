# Qualification native — 30 octobre 2026

Le moteur C# remplace les composants du relais : serveur MCP, workers de réveil/rangement, sidecar agentapi,
gardes, hooks, cartes et contrôle des applications. Cette fiche distingue les tests isolés, les observations du
poste et la bascule de production. Aucun message technique n'est envoyé à une autre IA pour qualifier ce chantier.

## Contrôles du code

`test.ps1` a publié le candidat hors OneDrive et terminé avec succès : deux autotests natifs, Ruff, Pyright et
577 tests pytest en 123,78 secondes, cas d'installation et de chemins physiques compris. Le contrôle complet final
est consigné dans `%LOCALAPPDATA%\NexusRelay\qualification\offline-delivered-20261030.log`.

- Les vingt contrats MCP Python sont figés en JSON embarqué ; chaque `inputSchema` et `outputSchema` est comparé,
  puis les sept sorties typées sont validées sur des résultats natifs, incidents compris.
- Les attentes groupées partagent une échéance et réservent toutes les demandes avant lecture. Une annulation,
  déconnexion ou relance ne consomme pas une réponse retenue ; un second propriétaire du même identifiant est refusé.
- Les workers exécutent les transports réels. Les fixtures interrompent une livraison entre l'intention durable
  et son acquittement, puis vérifient l'absence de rejeu, y compris si seule subsiste une trace d'événement incertain.
- Le sidecar appelle un agentapi isolé par son interface officielle : création, réutilisation, messages pour
  information joints à la prochaine demande, refus connu, résultat incertain et conversations remplacées.
- La sélection Codex vérifie l'UUID officiel, le chemin canonique et le libellé avant saisie et avant Entrée.
  Le rangement exige un identifiant explicitement vérifié dans le registre Relay. Les autres chats du même dossier,
  les enfants et les conversations courantes sont protégés. La reprise de l'historique exige aussi un véritable
  message utilisateur de réveil Relay associé au même identifiant de livraison ; un titre ou un dossier ne suffit pas.
- Les cartes utilisent le même moteur sur un navigateur local isolé et sur CDP : 18 scénarios vérifient notamment
  les menus, la restauration de vue, les masques privés, les cartes partielles et l'index des textes de l'application.
  Une fixture du serveur/worker vérifie l'actualisation automatique et l'aperçu effectivement exposé dans `status`.
- Le quota partagé NexusKeypad conserve la famille sélectionnée, la fraîcheur et les remises à zéro. Les fixtures
  couvrent une famille épuisée non sélectionnée, une sélection inconnue et les données anciennes.
- Les applications utilisateur passent par le Bureau Explorer. Un véritable Job Windows `KILL_ON_JOB_CLOSE`
  détruit l'appelant : l'application fixture lancée par le broker survit, tandis que le témoin hérité meurt.
  L'activité durable sans fin et l'activité illisible protègent l'application quel que soit leur âge. Un port absent
  autorise seulement une fermeture normale, après preuve d'inactivité et acquisition du jeton de premier plan.
  Les états de lancement `submitting` et `uncertain` interdisent tout nouvel arrêt ou rejeu avant confirmation.
- La relance du service est coopérative et adressée à un PID dont la naissance, le binaire, le module et le port sont
  vérifiés. Le test lance deux instances semblables sur des ports différents et confirme que seule la cible sort.
- Les gardes Python et C# acceptent deux enfants de la même session et refusent un autre chat. Les hooks natifs
  sont exercés par leur point d'entrée PowerShell stable, avec un chemin accentué et un signalement UTF-8.
- L'activation Antigravity répare une entrée de plugin absente, conserve les autres réglages et respecte tout
  choix explicite de désactivation. Aucun état de confiance Codex n'est fabriqué.

## Observations réelles du poste

L'outil officiel `list_projects` expose Documents et FMW. L'espace retenu explicitement pour les futurs réveils
Codex est le projet Documents existant, UUID noté localement, chemin
`%OneDrive%\Documents`. Il n'est pas nécessaire de créer un projet ou un chat.

Le candidat a inspecté ce projet dans Codex en lecture seule : un unique projet visible présente le même UUID,
chemin et libellé, et le compositeur est disponible. Aucun texte n'a été saisi ni envoyé.
Preuve locale : `%LOCALAPPDATA%\NexusRelay\qualification\route-observation-20261029\verification.json`.

Le quota réel lu dans le cache NexusKeypad sélectionnait Gemini : 20 % restants sur la fenêtre courte et 70 % sur
la semaine, sans blocage, malgré Argon épuisé. Le relevé conserve la date réelle de lecture du pad.
Le NexusLB installé a également répondu prêt par sa sonde JSON configurée.

Un candidat HTTP avec boîte vide et bases des applications installées en lecture seule a exécuté ses trois workers
et deux cycles de rangement sans erreur, sans geste ni envoi. Preuve locale :
`%LOCALAPPDATA%\NexusRelay\qualification\no-send-20261029-224502\verification.json`.
Cela valide le cas sans chat éligible. L'archivage positif observé précédemment sur un ancien chat Relay concernait
le moteur Python restauré. Après bascule, le même ancien root Relay a été vérifié par son véritable user wake,
un identifiant de message de Claude présent dans la boîte et une correspondance unique dans les racines de la base.
L'ancien événement `started` ne stockait pas encore de conversation ID. Seul ce root vérifié a été inscrit au registre
opérationnel, avec preuve et sauvegarde ; le migrateur automatique strict est resté inchangé. L'outil officiel l'a
désarchivé, puis le worker natif l'a réarchivé en quatre secondes (`codex-tidy`, une conversation, 23:37:54 UTC).
Aucun autre chat n'a été rendu éligible ni créé pour ce scénario.

## Bascule et limites de preuve

La bascule explicite est `install.ps1 -Runtime native`. `-PrepareOnly` compile et vérifie un candidat sans modifier
les processus, la spec ou les réglages installés. Une installation ordinaire préserve le runtime choisi localement.
Les exécutables sont immuables sous `%LOCALAPPDATA%\NexusRelay\versions\<empreinte>`.

Antigravity a terminé sa fermeture normale différée après un unique WM_CLOSE, sans arrêt forcé ni dialogue accepté.
Le nouveau processus est né du Bureau Explorer, indépendant de l'ancien Job du relais. Son vrai language server
lance le sidecar C#, et le superviseur Nexus actualisé lance le serveur C#. Le triplet Documents n'a été écrit
qu'après confirmation de la sortie du Python et de l'identité HTTP du premier processus natif. L'ancien ranger
n'a donc jamais reçu ce dossier. Claude reste l'hôte ; la date de reprise explicite conserve les anciens états.

Le chemin physique des artefacts est important : Codex MSIX redirige les nouveaux fichiers AppData dans son cache
de paquet. Déclarer le chemin logique produisait une erreur de fichier absent depuis Antigravity et le superviseur.
L'installateur utilise maintenant GetFinalPathNameByHandle et publie ce chemin physique ; les hooks vérifient aussi
la racine de produit installée. Leurs vrais hôtes ont confirmé le lancement du daemon et du sidecar.

Le vrai connecteur MCP du chat a répondu après cette reprise : hôte Claude, trois workers frais sans erreur,
sidecar vivant de runtime `native`, espace Documents explicite et aucun incident. Le quota réel de Gemini était
à 100 % restants sur la fenêtre courte et 70 % sur la semaine ; Argon épuisé restait visible sans bloquer Gemini.
L'identité et l'empreinte du binaire livré sont consignées dans la preuve locale de qualification.

Un réveil envoyé, sa réponse et son rangement complet ne sont pas prouvés par les seules compilations, inspections
ou fixtures. Aucun nouveau chat ni message à un pair n'a été créé pour ces tests. Les prochaines demandes réelles
de Claude passeront par les transports qualifiés ; leurs résultats resteront visibles dans `track` et `status`.

Les hooks Codex restent soumis à sa revue native. L'installateur suit les définitions des catalogues et ne remplace
pas cette revue par une clé de confiance écrite en interne. Référence officielle :
https://developers.openai.com/plugins/build/plugins#bundled-mcp-servers-and-lifecycle-hooks
