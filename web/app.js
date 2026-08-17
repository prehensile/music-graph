class KnowledgeGraphViewer {
    constructor() {
        this.graph = new graphology.Graph();
        this.sigma = null;
        this.neo4jDriver = null;
        this.ollamaUrl = (window.MUSIC_GRAPH_CONFIG || {}).ollamaUrl || 'http://localhost:11434';
        this.physics = {
            nodes: new Map(),
            running: false,
            damping: 0.85,
            repulsion: 800,
            attraction: 0.01,
            centerForce: 0.001,
            collisionEnabled: true,
            restitution: 1.0
        };
        this.dragging = null;
        this.wasDragging = false;
        this.init();
    }

    init() {
        this.initSigma();
        this.initNeo4j();
        this.setupEventListeners();
        this.loadDefaultData();
        this.startPhysicsSimulation();
    }

    initSigma() {
        const container = document.getElementById('graph-container');
        this.sigma = new Sigma(this.graph, container, {
            renderEdgeLabels: false,
            defaultNodeColor: '#ec4899',
            defaultEdgeColor: '#64748b',
            labelColor: { color: '#ffffff' },
            labelSize: 14,
            enableEdgeHoverEvents: true,
            enableNodeHoverEvents: true,
            allowInvalidContainer: true,
            labelRenderer: (context, data, settings) => {
                const size = settings.labelSize;
                const font = settings.labelFont;
                const weight = settings.labelWeight;
                
                context.fillStyle = '#ffffff';
                context.font = `${weight} ${size}px ${font}`;
                context.textAlign = 'center';
                context.textBaseline = 'middle';
                context.fillText(data.label, data.x, data.y);
            },
            hoverRenderer: (context, data, settings) => {
                const size = settings.labelSize;
                const font = settings.labelFont;
                const weight = settings.labelWeight;
                
                context.textAlign = 'center';
                context.textBaseline = 'middle';
                const textWidth = context.measureText(data.label).width;
                
                // Background
                context.fillStyle = 'rgba(0, 0, 0, 0.8)';
                context.fillRect(data.x - textWidth/2 - 3, data.y - size/2 - 2, textWidth + 6, size + 4);
                
                // Text
                context.fillStyle = '#ffffff';
                context.font = `${weight} ${size}px ${font}`;
                context.fillText(data.label, data.x, data.y);
            },
            nodeReducer: (node, data) => {
                return {
                    ...data,
                    size: data.size || 10,
                    color: data.color || '#ec4899'
                };
            },
            edgeReducer: (edge, data) => ({
                ...data,
                color: data.color || '#64748b',
                size: 1.5
            })
        });

        this.setupMouseInteractions();
        
        window.addEventListener('resize', () => {
            this.sigma.refresh();
        });
    }

    initNeo4j() {
        const config = window.MUSIC_GRAPH_CONFIG || {};
        const NEO4J_URI = config.neo4jUri || 'bolt://localhost:7687';
        const NEO4J_USER = config.neo4jUser || 'neo4j';
        const NEO4J_PASSWORD = config.neo4jPassword || '';

        if (!NEO4J_PASSWORD) {
            this.showError('No Neo4j password configured. Copy web/config.example.js to web/config.js and set your credentials.');
            return;
        }

        try {
            this.neo4jDriver = neo4j.driver(
                NEO4J_URI,
                neo4j.auth.basic(NEO4J_USER, NEO4J_PASSWORD)
            );
        } catch (error) {
            console.error('Failed to connect to Neo4j:', error);
            this.showError('Failed to connect to Neo4j database. Please check your connection settings.');
        }
    }

    setupEventListeners() {
        const queryInput = document.getElementById('query-input');
        const queryButton = document.getElementById('query-button');

        queryButton.addEventListener('click', () => this.executeQuery());
        
        queryInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                this.executeQuery();
            }
        });
    }

    async executeQuery() {
        const queryInput = document.getElementById('query-input');
        const queryButton = document.getElementById('query-button');
        const naturalLanguageQuery = queryInput.value.trim();

        if (!naturalLanguageQuery) {
            this.showError('Please enter a query');
            return;
        }

        queryButton.disabled = true;
        queryButton.textContent = 'Loading...';
        queryInput.disabled = true;

        try {
            const cypherQuery = await this.convertNaturalLanguageToCypher(naturalLanguageQuery);
            const result = await this.runNeo4jQuery(cypherQuery);
            this.renderGraphData(result);
        } catch (error) {
            console.error('Query execution failed:', error);
            this.showError('Query failed: ' + error.message);
        } finally {
            queryButton.disabled = false;
            queryButton.textContent = 'Go';
            queryInput.disabled = false;
        }
    }

    async convertNaturalLanguageToCypher(naturalQuery) {
        const trimmedQuery = naturalQuery.trim();
        const isSimpleName = /^[a-zA-Z0-9\s'&.-]+$/.test(trimmedQuery) && !trimmedQuery.includes('show') && !trimmedQuery.includes('find') && !trimmedQuery.includes('get') && !trimmedQuery.includes('all');
        
        if (isSimpleName) {
            return `MATCH (n)-[r]-(m) WHERE toLower(n.name) CONTAINS toLower('${trimmedQuery}') OR toLower(n.title) CONTAINS toLower('${trimmedQuery}') RETURN n, r, m LIMIT 50`;
        }

        const prompt = `You are a Neo4j Cypher query expert. Convert the following natural language query into a valid Cypher query.

Context: You are working with a knowledge graph that contains Artist and Master nodes from Discogs data. Nodes have properties like 'name' and 'title'.

Rules:
- ALWAYS return both the main nodes AND their neighbors with full properties
- Use this pattern: MATCH (n)-[r]-(m) RETURN n, r, m (not OPTIONAL MATCH)
- Use CONTAINS for partial string matching with toLower() for case-insensitive search
- Limit results to 50 or fewer
- For artist queries, use the Artist label specifically
- For master/release queries, use the Master label

Examples:
- "Goldie" → MATCH (n)-[r]-(m) WHERE toLower(n.name) CONTAINS toLower('goldie') OR toLower(n.title) CONTAINS toLower('goldie') RETURN n, r, m LIMIT 50
- "show me drum and bass artists" → MATCH (a:Artist)-[r]-(m) WHERE toLower(a.name) CONTAINS toLower('drum') OR toLower(a.name) CONTAINS toLower('bass') RETURN a, r, m LIMIT 50

Natural language query: "${naturalQuery}"

Respond with ONLY the Cypher query, no explanation:`;

        try {
            const response = await fetch(`${this.ollamaUrl}/api/generate`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    model: 'llama3.2',
                    prompt: prompt,
                    stream: false
                })
            });

            if (!response.ok) {
                throw new Error(`Ollama request failed: ${response.status}`);
            }

            const data = await response.json();
            const cypherQuery = data.response.trim();
            
            console.log('Generated Cypher:', cypherQuery);
            return cypherQuery;
        } catch (error) {
            console.error('Failed to convert query with Ollama:', error);
            return `MATCH (n) WHERE toLower(n.name) CONTAINS toLower('${naturalQuery}') OR toLower(n.title) CONTAINS toLower('${naturalQuery}') OPTIONAL MATCH (n)-[r]-(m) RETURN n, r, m LIMIT 50`;
        }
    }

    async runNeo4jQuery(cypherQuery) {
        if (!this.neo4jDriver) {
            throw new Error('Neo4j connection not available');
        }

        const session = this.neo4jDriver.session();
        try {
            const result = await session.run(cypherQuery);
            return result.records;
        } finally {
            await session.close();
        }
    }

    processNodeFromRecord(value, nodes, parentNodeId = null) {
        if (!value || !value.labels) return;
        
        const nodeId = value.identity.toString();
        if (nodes.has(nodeId) || this.graph.hasNode(nodeId)) return;
        
        const nodeType = value.labels[0];
        const labelProps = ['name', 'title'];
        let label = `Node ${nodeId}`;
        
        for (const prop of labelProps) {
            if (value.properties && value.properties[prop]) {
                label = value.properties[prop];
                break;
            }
        }
        
        // For additive rendering, spawn near the parent node being expanded
        let x = Math.random() * 1000;
        let y = Math.random() * 1000;
        let parentNode = null;
        
        // If we have a specific parent node ID, spawn near that node
        if (parentNodeId && this.graph.hasNode(parentNodeId)) {
            parentNode = parentNodeId;
            const parentAttrs = this.graph.getNodeAttributes(parentNodeId);
            
            // Calculate minimum spawn distance: parent radius + safety margin
            const parentRadius = parentAttrs.size / 2;
            const safetyMargin = 10;
            const minDistance = parentRadius + safetyMargin;
            const maxDistance = minDistance + 20; // Add some randomness
            
            // Generate random angle and distance
            const angle = Math.random() * 2 * Math.PI;
            const distance = minDistance + Math.random() * (maxDistance - minDistance);
            
            x = parentAttrs.x + Math.cos(angle) * distance;
            y = parentAttrs.y + Math.sin(angle) * distance;
        } else if (this.graph.order > 0) {
            // Fallback: spawn near a random existing node
            const existingNodes = this.graph.nodes();
            const randomExisting = existingNodes[Math.floor(Math.random() * existingNodes.length)];
            parentNode = randomExisting;
            const parentAttrs = this.graph.getNodeAttributes(randomExisting);
            
            // Same logic for fallback spawning
            const parentRadius = parentAttrs.size / 2;
            const safetyMargin = 10;
            const minDistance = parentRadius + safetyMargin;
            const maxDistance = minDistance + 20;
            
            const angle = Math.random() * 2 * Math.PI;
            const distance = minDistance + Math.random() * (maxDistance - minDistance);
            
            x = parentAttrs.x + Math.cos(angle) * distance;
            y = parentAttrs.y + Math.sin(angle) * distance;
        }
        
        nodes.set(nodeId, {
            id: nodeId,
            label: label,
            nodeType: nodeType,
            x: x,
            y: y,
            color: this.getNodeColor(nodeType),
            parentNode: parentNode
        });
    }




    renderGraphData(records, additive = false, parentNodeId = null) {
        if (!additive) {
            this.graph.clear();
        }
        
        const nodes = new Map();
        const relationships = [];

        records.forEach(record => {
            record.keys.forEach(key => {
                const value = record.get(key);
                
                if (value && typeof value === 'object') {
                    if (value.labels) {
                        this.processNodeFromRecord(value, nodes, parentNodeId);
                    } else if (value.type && value.start && value.end) {
                        relationships.push({
                            sourceId: value.start.toString(),
                            targetId: value.end.toString(),
                            type: value.type
                        });
                    }
                }
            });
        });

        nodes.forEach(node => {
            this.graph.addNode(node.id, {
                ...node,
                size: 15, // Will be updated with correct degree-based size below
                degree: 1,
                mass: 1
            });
        });

        relationships.forEach(rel => {
            if (this.graph.hasNode(rel.sourceId) && this.graph.hasNode(rel.targetId)) {
                if (!this.graph.hasEdge(rel.sourceId, rel.targetId)) {
                    this.graph.addEdge(rel.sourceId, rel.targetId, {
                        label: rel.type,
                        color: '#64748b',
                        size: 2
                    });
                }
            }
        });

        // Extract degree counts from Neo4j records
        const nodeDegrees = new Map();
        records.forEach(record => {
            record.keys.forEach(key => {
                if (key.endsWith('_degree')) {
                    const degreeValue = record.get(key);
                    const nodeKey = key.replace('_degree', '');
                    const nodeValue = record.get(nodeKey);
                    if (nodeValue && nodeValue.identity) {
                        const nodeId = nodeValue.identity.toString();
                        nodeDegrees.set(nodeId, degreeValue.toNumber ? degreeValue.toNumber() : degreeValue);
                    }
                }
            });
        });

        // Update labels, size, and mass with degree from Neo4j data
        nodes.forEach(node => {
            const degree = nodeDegrees.get(node.id) || 1;
            const size = this.calculateNodeSize(degree);
            const mass = this.calculateNodeMass(degree);
            
            this.graph.setNodeAttribute(node.id, 'label', node.label);
            this.graph.setNodeAttribute(node.id, 'size', size);
            this.graph.setNodeAttribute(node.id, 'degree', degree);
            this.graph.setNodeAttribute(node.id, 'mass', mass);
        });

        this.initPhysicsNodes();
        this.sigma.refresh();
        
        if (nodes.size === 0 && !additive) {
            this.showError('No data found for your query');
        }
    }

    async loadDefaultData() {
        try {
            const defaultQuery = `
                MATCH (a:Artist)-[r]-(m) 
                WHERE toLower(a.name) CONTAINS 'goldie'
                WITH a, r, m
                OPTIONAL MATCH (a)-[ra]-()
                OPTIONAL MATCH (m)-[rm]-()
                RETURN a, r, m, count(DISTINCT ra) as a_degree, count(DISTINCT rm) as m_degree
                LIMIT 50
            `;
            const result = await this.runNeo4jQuery(defaultQuery);
            this.renderGraphData(result);
        } catch (error) {
            console.error('Failed to load default data:', error);
        }
    }

    setupMouseInteractions() {
        // Disable panning
        this.sigma.setSetting('enableEdgePanning', false);
        this.sigma.setSetting('enableNodePanning', false);
        
        this.sigma.on('downNode', (e) => {
            this.dragging = {
                node: e.node,
                startX: e.event.x,
                startY: e.event.y,
                nodeX: this.graph.getNodeAttribute(e.node, 'x'),
                nodeY: this.graph.getNodeAttribute(e.node, 'y')
            };
            
            const physicsNode = this.physics.nodes.get(e.node);
            if (physicsNode) {
                physicsNode.pinned = true;
                physicsNode.vx = 0;
                physicsNode.vy = 0;
            }
        });

        this.sigma.on('clickNode', (e) => {
            if (!this.wasDragging) {
                this.expandNode(e.node);
               // this.animateToNode(e.node);
            }
            this.wasDragging = false;
        });

        this.sigma.on('enterNode', () => {
            document.body.style.cursor = 'pointer';
        });

        this.sigma.on('leaveNode', () => {
            document.body.style.cursor = 'default';
        });

        this.sigma.getMouseCaptor().on('mousemove', (e) => {
            if (this.dragging) {
                this.wasDragging = true;
                
                const pos = this.sigma.viewportToGraph(e);
                this.graph.setNodeAttribute(this.dragging.node, 'x', pos.x);
                this.graph.setNodeAttribute(this.dragging.node, 'y', pos.y);
                
                const physicsNode = this.physics.nodes.get(this.dragging.node);
                if (physicsNode) {
                    physicsNode.x = pos.x;
                    physicsNode.y = pos.y;
                }
                
                this.sigma.refresh();
            }
        });

        this.sigma.getMouseCaptor().on('mouseup', () => {
            if (this.dragging) {
                const physicsNode = this.physics.nodes.get(this.dragging.node);
                if (physicsNode) {
                    physicsNode.pinned = false;
                }
                this.dragging = null;
                
                setTimeout(() => {
                    this.wasDragging = false;
                }, 50);
            }
        });
    }

    getNodeColor(label) {
        const colors = {
            'Artist': '#3b82f6',
            'Master': '#ec4899',
            'Person': '#ef4444',
            'Organization': '#6366f1',
            'Location': '#10b981',
            'Event': '#f59e0b',
            'Concept': '#8b5cf6',
            'Document': '#64748b',
            'default': '#6b7280'
        };
        return colors[label] || colors['default'];
    }

    calculateNodeSize(degree) {
        // Logarithmic scaling: log(degree + 1) to handle degree 0
        const logScale = Math.log(degree + 1) * 8;
        return Math.max(6, Math.min(50, logScale + 8));
    }

    calculateNodeMass(degree) {
        return Math.max(0.5, degree * 0.3);
    }

    initPhysicsNodes() {
        this.graph.forEachNode((nodeId, attributes) => {
            if (!this.physics.nodes.has(nodeId)) {
                const mass = attributes.mass || this.calculateNodeMass(attributes.degree || 1);
                
                this.physics.nodes.set(nodeId, {
                    x: attributes.x,
                    y: attributes.y,
                    vx: 0,
                    vy: 0,
                    mass: mass,
                    pinned: false
                });
            }
        });
    }

    startPhysicsSimulation() {
        if (this.physics.running) return;
        this.physics.running = true;
        
        const animate = () => {
            if (!this.physics.running) return;
            
            this.updatePhysics();
            this.sigma.refresh();
            requestAnimationFrame(animate);
        };
        
        requestAnimationFrame(animate);
    }

    updatePhysics() {
        const centerX = 0;
        const centerY = 0;
        
        this.physics.nodes.forEach((physicsNode, nodeId) => {
            if (physicsNode.pinned) return;
            
            let fx = 0, fy = 0;
            
            this.physics.nodes.forEach((otherNode, otherNodeId) => {
                if (nodeId === otherNodeId) return;
                
                const dx = physicsNode.x - otherNode.x;
                const dy = physicsNode.y - otherNode.y;
                const distance = Math.sqrt(dx * dx + dy * dy) || 1;
                
                let repulsion = this.physics.repulsion / (distance * distance);
                
                // Scale repulsion by masses - higher mass nodes repel more strongly
                const nodeMass = physicsNode.mass;
                const otherMass = this.physics.nodes.get(otherNodeId).mass;
                const massEffect = Math.sqrt(nodeMass * otherMass) * 2;
                repulsion *= massEffect;
                
                fx += (dx / distance) * repulsion / physicsNode.mass;
                fy += (dy / distance) * repulsion / physicsNode.mass;
            });
            
            this.graph.neighbors(nodeId).forEach(neighborId => {
                const neighborNode = this.physics.nodes.get(neighborId);
                if (!neighborNode) return;
                
                const dx = neighborNode.x - physicsNode.x;
                const dy = neighborNode.y - physicsNode.y;
                const distance = Math.sqrt(dx * dx + dy * dy) || 1;
                const idealDistance = 100;
                const spring = (distance - idealDistance) * this.physics.attraction;
                
                fx += (dx / distance) * spring;
                fy += (dy / distance) * spring;
            });
            
            const centerDx = centerX - physicsNode.x;
            const centerDy = centerY - physicsNode.y;
            fx += centerDx * this.physics.centerForce;
            fy += centerDy * this.physics.centerForce;
            
            physicsNode.vx = (physicsNode.vx + fx) * this.physics.damping;
            physicsNode.vy = (physicsNode.vy + fy) * this.physics.damping;
            
            physicsNode.x += physicsNode.vx;
            physicsNode.y += physicsNode.vy;
            
            this.graph.setNodeAttribute(nodeId, 'x', physicsNode.x);
            this.graph.setNodeAttribute(nodeId, 'y', physicsNode.y);
        });
        
        // Handle collisions
        if (this.physics.collisionEnabled) {
            this.handleCollisions();
        }
    }

    handleCollisions() {
        const nodeIds = Array.from(this.physics.nodes.keys());
        
        for (let i = 0; i < nodeIds.length; i++) {
            for (let j = i + 1; j < nodeIds.length; j++) {
                const nodeId1 = nodeIds[i];
                const nodeId2 = nodeIds[j];
                
                const node1 = this.physics.nodes.get(nodeId1);
                const node2 = this.physics.nodes.get(nodeId2);
                
                if (node1.pinned && node2.pinned) continue;
                
                const attrs1 = this.graph.getNodeAttributes(nodeId1);
                const attrs2 = this.graph.getNodeAttributes(nodeId2);
                
                const dx = node2.x - node1.x;
                const dy = node2.y - node1.y;
                const distance = Math.sqrt(dx * dx + dy * dy);
                
                const minDistance = (attrs1.size + attrs2.size) / 2 + 2; // Add small buffer
                
                if (distance < minDistance && distance > 0) {
                    // Collision detected
                    const overlap = minDistance - distance;
                    const separationX = (dx / distance) * overlap * 0.5;
                    const separationY = (dy / distance) * overlap * 0.5;
                    
                    // Separate the nodes
                    if (!node1.pinned) {
                        node1.x -= separationX;
                        node1.y -= separationY;
                        this.graph.setNodeAttribute(nodeId1, 'x', node1.x);
                        this.graph.setNodeAttribute(nodeId1, 'y', node1.y);
                    }
                    
                    if (!node2.pinned) {
                        node2.x += separationX;
                        node2.y += separationY;
                        this.graph.setNodeAttribute(nodeId2, 'x', node2.x);
                        this.graph.setNodeAttribute(nodeId2, 'y', node2.y);
                    }
                    
                    // Calculate collision response (rebound)
                    const relativeVx = node2.vx - node1.vx;
                    const relativeVy = node2.vy - node1.vy;
                    
                    const normalX = dx / distance;
                    const normalY = dy / distance;
                    
                    const relativeVelocityInNormal = relativeVx * normalX + relativeVy * normalY;
                    
                    // Don't resolve if velocities are separating
                    // if (relativeVelocityInNormal > 0) continue;
                    
                    const restitution = this.physics.restitution;
                    const impulse = -(1 + restitution) * relativeVelocityInNormal;
                    const totalMass = node1.mass + node2.mass;
                    
                    const impulseX = impulse * normalX;
                    const impulseY = impulse * normalY;
                    
                    if (!node1.pinned) {
                        node1.vx -= impulseX * (node2.mass / totalMass);
                        node1.vy -= impulseY * (node2.mass / totalMass);
                    }
                    
                    if (!node2.pinned) {
                        node2.vx += impulseX * (node1.mass / totalMass);
                        node2.vy += impulseY * (node1.mass / totalMass);
                    }
                }
            }
        }
    }

    async expandNode(nodeId) {
        try {
            const query = `
                MATCH (center)-[r1]-(neighbor)
                WHERE id(center) = ${nodeId}
                WITH center, r1, neighbor
                OPTIONAL MATCH (center)-[rc]-()
                OPTIONAL MATCH (neighbor)-[rn]-()
                RETURN center, r1, neighbor, count(DISTINCT rc) as center_degree, count(DISTINCT rn) as neighbor_degree
                LIMIT 50
            `;
            const result = await this.runNeo4jQuery(query);
            this.renderGraphData(result, true, nodeId);
        } catch (error) {
            console.error('Failed to expand node:', error);
            this.showError('Failed to expand node: ' + error.message);
        }
    }

    animateToNode(nodeId) {
        const nodeAttributes = this.graph.getNodeAttributes(nodeId);
        const camera = this.sigma.getCamera();
        
        const targetX = nodeAttributes.x;
        const targetY = nodeAttributes.y;
        const targetRatio = 0.8; // Zoom level
        
        camera.animate(
            { x: targetX, y: targetY, ratio: targetRatio },
            { duration: 800, easing: 'quadInOut' }
        );
    }

    showError(message) {
        console.error(message);
        alert(message);
    }
}

document.addEventListener('DOMContentLoaded', () => {
    new KnowledgeGraphViewer();
});